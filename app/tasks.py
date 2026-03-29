import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)

# Maximum wall-clock time for a single system task execution (10 minutes)
_SYSTEM_TASK_TIMEOUT = int(os.environ.get("SYSTEM_TASK_TIMEOUT", 600))

# Matches failure indicators only when they appear as standalone signals,
# not inside negations like "no errors" or "without error".
_FAILURE_RE = re.compile(
    r'(?<!\bno\s)(?<!\bno\b)(?<!\bwithout\s)'
    r'\b(error|failed|failure|Guardrail Intervention:|not available)\b',
    re.IGNORECASE,
)


async def run_scheduled_task(task_prompt: str, notify: dict, is_actionable: bool = False):
    """
    Executed by APScheduler when a scheduled task fires.
    Runs the agent with the task prompt and delivers the response
    to the user via their original messaging channel.
    """
    from app.core.agent_executor import extract_agent_response
    from run_bot import get_runner

    runner = get_runner()

    # Build the response — either from the agent or just the raw prompt for simple reminders
    if not is_actionable:
        response = f"Reminder: {task_prompt}"
    elif runner:
        user_id = "system_scheduler"
        session_id = "scheduled_task"
        query = (
            f"Scheduled Task: {task_prompt}\n"
            "(This is an automated reminder. Execute the task or deliver the reminder to the user. "
            "Do not ask for missing credentials; stop gracefully if something is missing.)"
        )

        try:
            # Ensure session exists
            try:
                session = await runner.session_service.get_session(
                    app_name=runner.app_name, user_id=user_id, session_id=session_id
                )
                if session is None:
                    await runner.session_service.create_session(
                        app_name=runner.app_name, user_id=user_id, session_id=session_id
                    )
            except Exception:
                await runner.session_service.create_session(
                    app_name=runner.app_name, user_id=user_id, session_id=session_id
                )

            response = await extract_agent_response(runner, user_id, session_id, query)
            if "Guardrail Intervention:" in response:
                response = f"Reminder: {task_prompt}\n\n[Warning]: {response}"
        except Exception:
            logger.exception("Scheduled task agent execution failed")
            response = f"Reminder: {task_prompt}"
    else:
        response = f"Reminder: {task_prompt}"

    # Deliver to the user's channel
    await _deliver_message(notify, response)


async def run_system_task(task_prompt: str, notify: dict, admin_user_id: str, silent: bool = False):
    """
    Executed by APScheduler for admin-only system maintenance tasks.
    Runs the agent with full privileges in an isolated session, then cleans up.

    Unlike run_scheduled_task, this:
      - Creates a fresh session per execution (no cross-task contamination)
      - Injects the admin's identity so guardrails pass
      - Supports silent mode (only notifies on failure/warnings)
      - Always runs the agent (no plain-text reminder path)
    """
    import uuid

    from app.core.agent_executor import extract_agent_response, update_session_state
    from run_bot import get_runner

    runner = get_runner()
    if not runner:
        logger.error("System task failed: runner not available. Task: %s", task_prompt)
        await _deliver_message(notify, f"System Task Failed: Runner not available.\nTask: {task_prompt}")
        return

    # Re-validate admin privileges at execution time (may have been revoked since scheduling)
    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]
    if admin_users and admin_user_id not in admin_users:
        logger.warning(
            "System task aborted: admin_user_id %s is no longer authorized. Task: %s",
            admin_user_id, task_prompt,
        )
        await _deliver_message(
            notify,
            f"System Task Aborted: Admin '{admin_user_id}' is no longer authorized.\nTask: {task_prompt}",
        )
        return

    # Isolated session — created fresh, deleted after execution
    session_id = f"sys_task_{uuid.uuid4().hex[:8]}"
    user_id = "system_admin"

    query = (
        f"System Maintenance Task: {task_prompt}\n"
        "(This is an automated system task running with admin privileges. "
        "Execute the task fully. Report results clearly. "
        "Do not ask for missing credentials; stop gracefully if something is missing.)"
    )

    try:
        # Create the ephemeral session
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        # Inject admin identity so guardrails recognize this as an admin execution
        await update_session_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            state_delta={"user_id": admin_user_id},
        )

        response = await asyncio.wait_for(
            extract_agent_response(runner, user_id, session_id, query),
            timeout=_SYSTEM_TASK_TIMEOUT,
        )

        is_failure = bool(_FAILURE_RE.search(str(response)))

        if silent and not is_failure:
            logger.info("System task completed silently: %s", task_prompt)
        else:
            prefix = "System Task Report" if not is_failure else "System Task Warning"
            await _deliver_message(notify, f"{prefix}:\n{response}")

    except asyncio.TimeoutError:
        logger.error("System task timed out after %ds: %s", _SYSTEM_TASK_TIMEOUT, task_prompt)
        await _deliver_message(
            notify,
            f"System Task Timed Out (>{_SYSTEM_TASK_TIMEOUT}s):\nTask: {task_prompt}",
        )
    except Exception:
        logger.exception("System task execution failed: %s", task_prompt)
        await _deliver_message(notify, f"System Task Failed:\nTask: {task_prompt}\nCheck logs for details.")
    finally:
        # Clean up the ephemeral session
        try:
            await runner.session_service.delete_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
        except Exception:
            pass


async def _deliver_message(notify: dict, message: str):
    """Send a message to the user via their original channel using the adapter registry."""
    from app.core.transport import get_adapter

    if not notify:
        logger.warning("Scheduled task fired but no notification channel configured")
        return

    channel_type = notify.get("type")
    adapter = get_adapter(channel_type)
    if adapter:
        target = notify.get("chat_id") or notify.get("channel")
        await adapter.send_message(target, message)
    else:
        logger.warning("No adapter registered for channel type: %s", channel_type)
