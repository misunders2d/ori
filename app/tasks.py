import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# In-memory registry for tracking real-time status of background tasks
ACTIVE_TASKS = {}

# Persistent event log for scheduled/system task fires — appended JSONL so the
# agent can read it back via get_scheduled_task_logs without parsing free-form
# Python logs. Rotation is handled by simple size-based truncation.
_JOB_LOG_PATH = os.path.abspath("./data/scheduler_jobs.log")
_JOB_LOG_MAX_BYTES = 500_000


def _log_job_event(event: str, **fields) -> None:
    """Append one JSON line to the scheduler job log.

    Events: fire_start, fire_end, delivery, error. Fields should include
    job_id, task_id, and event-specific context (prompt_preview, next_run,
    duration_ms, error, channel).
    """
    try:
        os.makedirs(os.path.dirname(_JOB_LOG_PATH), exist_ok=True)
        try:
            if os.path.getsize(_JOB_LOG_PATH) > _JOB_LOG_MAX_BYTES:
                # Keep the last half — cheap truncation, no rotation files.
                with open(_JOB_LOG_PATH, "rb") as f:
                    data = f.read()
                keep = data[len(data) // 2 :].split(b"\n", 1)
                tail = keep[1] if len(keep) == 2 else b""
                with open(_JOB_LOG_PATH, "wb") as f:
                    f.write(tail)
        except FileNotFoundError:
            pass
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with open(_JOB_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to write scheduler job log: %s", e)


# Plan-driven continuation is handled by the Workflow root —
# `plan_executor_workflow` (app/workflows/plan_executor.py) wraps the
# coordinator in a dynamic-workflow @node that loops while
# plan_storage.has_pending_steps is True. Scheduled tasks invoke the
# runner the same way interactive chat does and inherit the loop for
# free. No external pumping needed here.


async def run_scheduled_task(
    task_prompt: str,
    notify: dict,
    owner_user_id: str,
    task_id: str | None = None,
    steps: list[str] | None = None,
):
    """
    Executed by APScheduler when a scheduled task fires.

    owner_user_id is the creator's platform identifier (e.g. 'tg_330959414' or
    'sergey@mellanni.com') and is required — scheduling tools refuse to create
    tasks without one, so a missing value here indicates a corrupted job from
    before this contract was introduced. Such jobs are wiped by the
    scripts/reset_scheduled_tasks.py migration; if you see the warning below
    in production, run that script and have the creator reschedule.

    If `steps` is provided (enforced task), the plan is seeded into storage
    before the agent runs — the plan_executor workflow then drives the
    step loop in code (`get_next_step` / coordinator turn / `complete_step`),
    so the LLM cannot skip, reorder, or paraphrase steps. If `steps` is
    None, the task runs under normal LLM-decided flow.
    """
    import uuid

    from app.runtime.executor import extract_agent_response
    from run_bot import get_runner

    if not task_id:
        task_id = f"sched_{uuid.uuid4().hex[:8]}"

    start_ts = datetime.now()
    ACTIVE_TASKS[task_id] = {
        "prompt": task_prompt,
        "type": "scheduled",
        "status": "Running",
        "start_time": start_ts.isoformat(),
        "end_time": None,
        "error": None
    }
    _log_job_event(
        "fire_start",
        task_id=task_id,
        kind="scheduled",
        prompt_preview=task_prompt[:140],
        channel=(notify or {}).get("chat_id") or (notify or {}).get("channel"),
    )

    runner = get_runner()

    if runner:
        # Ephemeral session per fire — isolates plan state, conversation history,
        # and scratchpad between concurrent/sequential scheduled tasks.
        user_id = "system_scheduler"
        session_id = task_id  # task_id already carries a 'sched_' or 'immediate_' prefix
        query = (
            f"Scheduled Task: {task_prompt}\n"
            "(This is an automated reminder. Execute the task or deliver the reminder to the user. "
            "Do not ask for missing credentials; stop gracefully if something is missing.)"
        )

        # The scheduler runs tasks under a synthetic user_id ("system_scheduler")
        # so the session is isolated from the owner's chat. But per-user tools
        # (Google, preferences, admin checks) need state["user_id"] to be the
        # real owner — we inject it via actual_caller_id, which state_setter
        # then promotes into session state. owner_user_id is a required kwarg
        # on this function; if it's missing, the job is stale (created before
        # this contract) and no per-user tool can succeed.
        if not owner_user_id:
            logger.error(
                "Scheduled task %s has no owner_user_id. The job was persisted "
                "before the explicit-owner contract was introduced. Wipe legacy "
                "jobs with scripts/reset_scheduled_tasks.py and have the creator "
                "reschedule. Failing the task.",
                task_id,
            )
            response = (
                f":x: Scheduled task `{task_id}` failed: no owner recorded on this job. "
                f"Please reschedule this task (the legacy entry pre-dates the identity fix)."
            )
            ACTIVE_TASKS[task_id]["status"] = "Failed (no owner)"
            ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
            _log_job_event("error", task_id=task_id, kind="scheduled", error="missing owner_user_id")
            await _deliver_with_fallback(notify, response, task_id=task_id)
            return

        try:
            await runner.session_service.create_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )

            # Enforced task: seed the plan BEFORE the agent's first turn so
            # the plan_executor workflow's step loop sees pending steps on
            # iteration 0 and drives execution step-by-step from there.
            if steps:
                from app.tools.planner import seed_plan_for_session
                await seed_plan_for_session(session_id, task_prompt[:500], steps)
                logger.info(
                    "Scheduled task %s: seeded enforced plan with %d step(s)",
                    task_id, len(steps),
                )

            # Plan continuation (when steps were seeded) is handled inside
            # the workflow root — see app/workflows/plan_executor.py. The
            # @node loops while has_pending_steps is True, so a single
            # extract_agent_response call drives the entire enforced plan
            # to completion.
            agent_response = await extract_agent_response(
                runner, user_id, session_id, query,
                actual_caller_id=owner_user_id or None,
            )
            response = agent_response.text if hasattr(agent_response, "text") else str(agent_response)
            if not response or not response.strip():
                # Agent returned empty — treat as failure so user sees something.
                response = (
                    f":warning: Scheduled task `{task_id}` produced an empty response.\n"
                    f"Prompt: {task_prompt[:200]}"
                )
                ACTIVE_TASKS[task_id]["status"] = "Failed (empty response)"
                _log_job_event("error", task_id=task_id, kind="scheduled", error="empty response")
            elif "Guardrail Intervention:" in response:
                response = f":warning: Scheduled task `{task_id}` hit a guardrail.\n{response}"
                ACTIVE_TASKS[task_id]["status"] = "Failed (guardrail)"
                _log_job_event("error", task_id=task_id, kind="scheduled", error="guardrail intervention")
            else:
                ACTIVE_TASKS[task_id]["status"] = "Completed"
            ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        except Exception as e:
            logger.exception("Scheduled task agent execution failed")
            response = (
                f":x: Scheduled task `{task_id}` failed.\n"
                f"Prompt: {task_prompt[:200]}\n"
                f"Error: {type(e).__name__}: {e}"
            )
            ACTIVE_TASKS[task_id]["status"] = "Failed"
            ACTIVE_TASKS[task_id]["error"] = str(e)
            ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
            _log_job_event("error", task_id=task_id, kind="scheduled", error=f"{type(e).__name__}: {e}")
        finally:
            try:
                await runner.session_service.delete_session(
                    app_name=runner.app_name, user_id=user_id, session_id=session_id
                )
            except Exception:
                pass
    else:
        # Runner unavailable — bot is starting up or shutting down. Report honestly.
        response = (
            f":x: Scheduled task `{task_id}` could not run: agent runner unavailable.\n"
            f"Prompt: {task_prompt[:200]}"
        )
        ACTIVE_TASKS[task_id]["status"] = "Failed (no runner)"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        _log_job_event("error", task_id=task_id, kind="scheduled", error="runner unavailable")

    # Deliver to the user's channel, with fallback to origin on failure.
    await _deliver_with_fallback(notify, response, task_id=task_id)
    duration_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
    _log_job_event(
        "fire_end",
        task_id=task_id,
        kind="scheduled",
        status=ACTIVE_TASKS[task_id]["status"],
        duration_ms=duration_ms,
        response_preview=(response or "")[:200],
    )


async def run_system_task(
    task_prompt: str,
    notify: dict,
    admin_user_id: str,
    silent: bool = False,
    task_id: str | None = None,
    steps: list[str] | None = None,
):
    """
    Executed by APScheduler for admin-only system maintenance tasks.
    Runs the agent with full privileges in an isolated session, then cleans up.

    If `steps` is provided (enforced task), the plan is seeded into storage
    before the agent runs — the plan_executor workflow drives execution
    step-by-step (LLM cannot skip, reorder, or paraphrase). If `steps` is
    None, normal LLM-decided flow.
    """
    import uuid

    from app.runtime.executor import extract_agent_response
    from run_bot import get_runner

    if not task_id:
        task_id = f"sys_{uuid.uuid4().hex[:8]}"

    logger.info("System Task: Starting %s (%s)", task_id, task_prompt)

    start_ts = datetime.now()
    ACTIVE_TASKS[task_id] = {
        "prompt": task_prompt,
        "type": "system",
        "status": "Running",
        "start_time": start_ts.isoformat(),
        "end_time": None,
        "error": None
    }
    _log_job_event(
        "fire_start",
        task_id=task_id,
        kind="system",
        prompt_preview=task_prompt[:140],
        admin_user_id=admin_user_id,
        silent=silent,
    )

    runner = get_runner()
    if not runner:
        logger.error("System task failed: runner not available. Task: %s", task_prompt)
        ACTIVE_TASKS[task_id]["status"] = "Failed"
        ACTIVE_TASKS[task_id]["error"] = "Runner not available"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        await _deliver_message(notify, f"System Task Failed: Runner not available.\nTask: {task_prompt}")
        return

    # Isolated session — created fresh, deleted after execution
    session_id = f"sys_task_{uuid.uuid4().hex[:8]}"
    user_id = "system_admin"

    query = (
        f"System Maintenance Task: {task_prompt}\n"
        "(This is an automated system task running with admin privileges. "
        "Execute the task fully. Report results clearly. "
        "Do not ask for missing credentials; stop gracefully if something is missing. "
        "CRITICAL: Before completing this task, you MUST use the `remember_info` tool to store a concise summary "
        "of your final result (Success or Failure cause) in the 'background_tasks' category, so the user can query it later.)"
    )

    try:
        # Create the ephemeral session
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        # Enforced task: seed the plan before first turn.
        if steps:
            from app.tools.planner import seed_plan_for_session
            await seed_plan_for_session(session_id, task_prompt[:500], steps)
            logger.info(
                "System task %s: seeded enforced plan with %d step(s)",
                task_id, len(steps),
            )

        # Pass admin identity via actual_caller_id so StateInitializerPlugin
        # picks it up.
        logger.info("System Task: Executing agent for %s", task_id)
        # Plan continuation is handled by the Workflow root.
        agent_response = await extract_agent_response(
            runner, user_id, session_id, query, actual_caller_id=admin_user_id
        )
        response = agent_response.text if hasattr(agent_response, "text") else str(agent_response)

        is_failure = any(
            indicator in response
            for indicator in ["error", "Error", "failed", "Failed", "Guardrail Intervention:", "not available"]
        )

        if silent and not is_failure:
            logger.info("System task completed silently: %s", task_prompt)
        else:
            prefix = "System Task Report" if not is_failure else "System Task Warning"
            msg = f"{prefix}:\n{response}"
            logger.info("System Task: Delivering report for %s", task_id)
            await _deliver_with_fallback(notify, msg, task_id=task_id)

        ACTIVE_TASKS[task_id]["status"] = "Completed" if not is_failure else "Completed (With Warnings)"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        logger.info("System Task: Finished %s", task_id)

    except Exception as e:
        logger.exception("System task execution failed: %s", task_prompt)
        ACTIVE_TASKS[task_id]["status"] = "Failed"
        ACTIVE_TASKS[task_id]["error"] = str(e)
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        _log_job_event("error", task_id=task_id, kind="system", error=str(e))
        await _deliver_with_fallback(notify, f"System Task Failed:\nTask: {task_prompt}\nCheck logs for details.", task_id=task_id)
    finally:
        # Clean up the ephemeral session
        try:
            await runner.session_service.delete_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
        except Exception:
            pass
        duration_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
        _log_job_event(
            "fire_end",
            task_id=task_id,
            kind="system",
            status=ACTIVE_TASKS[task_id].get("status", "unknown"),
            duration_ms=duration_ms,
        )


async def _deliver_message(notify: dict, message: str, task_id: str = "") -> bool:
    """Send a message to the user's chat AND inject it into their session history.

    Returns True on successful send, False on any failure (no adapter, adapter raised,
    channel not delivered). Failures are logged to the scheduler job log so the user
    can diagnose them via get_scheduled_task_logs.

    The injection is what makes scheduled task output visible to the agent on the
    user's next turn.
    """
    from app.runtime.transport import get_adapter

    if not notify:
        logger.warning("Notification delivery skipped: no notification info")
        _log_job_event("delivery_failure", task_id=task_id, reason="no notify dict")
        return False

    channel_type = notify.get("type")
    adapter = get_adapter(channel_type)
    target = notify.get("chat_id") or notify.get("channel")
    if not adapter:
        logger.warning("No adapter registered for channel type: %s", channel_type)
        _log_job_event("delivery_failure", task_id=task_id, reason=f"no adapter for {channel_type}", target=target)
        return False

    logger.info("Delivering message to %s channel, target: %s", channel_type, target)
    try:
        await adapter.send_message(target, message)
        delivered = True
    except Exception as e:
        logger.error("Failed to deliver message via adapter: %s", e)
        _log_job_event("delivery_failure", task_id=task_id, reason=str(e), target=target)
        delivered = False

    # Mirror the delivered message into the chat's session so the model has it
    # in conversation history when the user asks a follow-up.
    await _inject_into_session(notify, message)
    return delivered


async def _deliver_with_fallback(notify: dict, message: str, task_id: str) -> None:
    """Deliver to notify's channel; if that fails and origin_session_id differs,
    try delivering the failure notice to origin so the user is never left in the
    dark. Never raises.
    """
    delivered = await _deliver_message(notify, message, task_id=task_id)
    if delivered:
        return

    # Fallback — try origin_session_id if it's a different channel.
    origin = (notify or {}).get("origin_session_id", "")
    primary = (notify or {}).get("chat_id") or (notify or {}).get("channel", "")
    if not origin or not primary:
        return
    if origin == f"sl_{primary}" or origin == f"tg_{primary}" or origin.endswith(f"_{primary}"):
        return  # same channel as primary, nothing to retry

    from app.runtime.transport import parse_notify_from_session_id
    fallback_notify = parse_notify_from_session_id(origin)
    if not fallback_notify:
        return

    fallback_msg = (
        f":warning: Could not deliver scheduled task `{task_id}` to its target channel. "
        f"Routing to the session that scheduled it.\n\n{message}"
    )
    await _deliver_message(fallback_notify, fallback_msg, task_id=task_id)


async def _inject_into_session(notify: dict, message: str):
    """Append the delivered scheduled-task text as a model-authored event in the
    target chat's session, so it shows up in conversation history on the next turn.

    Silently no-ops if the session doesn't exist or the runner isn't available.
    """
    target_session = (notify or {}).get("origin_session_id")
    if not target_session:
        return

    from run_bot import get_runner

    runner = get_runner()
    if not runner:
        return

    try:
        import time as _time
        import uuid as _uuid

        from google.adk.events.event import Event
        from google.genai import types as _types

        # In this codebase, ADK user_id and session_id are the same value for
        # chat sessions (see telegram_poller / slack_poller: session_user_id = session_id).
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=target_session, session_id=target_session
        )
        if session is None:
            return  # Chat hasn't started its session yet — nothing to append to.

        content = _types.Content(
            role="model",
            parts=[_types.Part.from_text(text=message)],
        )
        event = Event(
            id=str(_uuid.uuid4()),
            author="scheduler",
            timestamp=_time.time(),
            content=content,
        )
        await runner.session_service.append_event(session, event)
        logger.info("Injected scheduled task message into session %s", target_session)
    except Exception as e:
        logger.warning("Failed to inject scheduled message into session %s: %s", target_session, e)
