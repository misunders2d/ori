import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# In-memory registry for tracking real-time status of background tasks
ACTIVE_TASKS = {}


def _read_bytes(path: str) -> bytes:
    """Synchronous file read helper, used inside ``asyncio.to_thread``
    so multi-MB media payloads don't stall the asyncio event loop
    during a single ``open().read()`` call."""
    with open(path, "rb") as f:
        return f.read()

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


# Max re-invocations for plan-driven continuation. A plan with N steps typically
# needs N+1 turns (N executions + final summary), but tool failures and retries
# can extend this. 25 leaves comfortable slack for a 6-step plan while capping
# runaway costs if the agent gets stuck in a loop.
_MAX_PLAN_ITERATIONS = 25

_PLAN_CONTINUATION_PROMPT = (
    "Your enforced plan still has pending steps. Do NOT emit a user-facing "
    "summary yet. Call get_next_step immediately and continue executing. "
    "Only produce a final summary after complete_step reports "
    "'All steps completed!'."
)


def _cleanup_ephemeral_task_state(session_id: str) -> None:
    """Best-effort cleanup for scheduler-owned session sidecar state."""
    try:
        from app.tools.scratchpad import cleanup_session_scratchpads

        cleanup_session_scratchpads(session_id)
    except Exception:
        logger.warning(
            "Scratchpad cleanup failed for scheduled session %s",
            session_id, exc_info=True,
        )

    try:
        plan_path = os.path.join(os.path.abspath("./tmp/plans"), f"{session_id}.json")
        if os.path.exists(plan_path):
            os.remove(plan_path)
    except Exception:
        logger.warning(
            "Plan cleanup failed for scheduled session %s",
            session_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# v1fix Slice-1 — durable, D5-correct, reliably-firing scheduled path.
#
# The scheduled fire path used to fabricate an ephemeral ADK session per fire
# (user_id="system_scheduler", session_id=task_id), delete+recreate it every
# run, and wipe its sidecar — so cross-run progress, crash-resume, and
# never-double-send were structurally impossible, and a fire delayed past the
# process-global misfire_grace_time was silently dropped.
#
# Slice-1 re-homes the run onto a DURABLE side-session LINKED to the creating
# chat (session_id = "<chat_session>::job::<job_key>", under the chat's
# user_id), run through the SAME boot Runner (run_bot.get_runner(), app=ori_app)
# — so the scheduled turn resolves under the SAME (app_name) ADK identity the
# live chat uses (D5: app_name divergence => orphan row => in-chat follow-up
# silently breaks). A logical-occurrence cursor kept in the durable session
# state makes catch-up after downtime never-skip + never-double-send.
#
# NOT in Slice-1 (LATER, gated): the per-job SequentialAgent/LoopAgent builder,
# per-step fidelity (QB), defer-blocks-downstream (QC), typed multi-target /
# strict-append. Slice-1 is the reliability CORE only.
# ---------------------------------------------------------------------------

# Hard caps so very-long downtime can't enumerate/track unbounded occurrences.
_MAX_CATCHUP_OCCURRENCES = 500
_MAX_DELIVERED_TRACKED = 500


def _stable_job_key(job_id: str | None, owner_user_id: str, notify: dict, task_prompt: str) -> str:
    """A stable identifier for the durable side-session.

    Prefers the APScheduler job_id (threaded through from the scheduling
    tools). Legacy jobs persisted before this contract carry no job_id kwarg —
    derive a deterministic key from owner + origin + prompt so those jobs still
    get a CONSISTENT durable session across fires (v1 prod keeps working) rather
    than crashing or churning a fresh session every fire.
    """
    if job_id:
        return job_id
    origin = (notify or {}).get("origin_session_id") or (notify or {}).get("target_session_id") or ""
    raw = f"{owner_user_id}|{origin}|{task_prompt}".encode("utf-8", "replace")
    return "legacy_" + hashlib.sha1(raw).hexdigest()[:12]


def _chat_identity_from_notify(notify: dict) -> str | None:
    """Recover the creating chat's ADK session id at fire time.

    No new schedule-time plumbing: the chat identity is already on the job in
    notify.origin_session_id (stamped by _stamp_ownership). For chat sessions
    ADK user_id == session_id == channel (telegram_poller / slack_poller), so
    the single origin value is both. Returns None when it can't be recovered
    (legacy notify dict) — caller falls back to an isolated durable session.
    """
    return (notify or {}).get("origin_session_id") or None


def _linked_side_session_id(chat_session_id: str | None, job_key: str) -> str:
    """Durable side-session id, LINKED to the chat but ISOLATED from the live
    chat turn (QA-locked: avoids collision with the user typing mid-fire)."""
    return f"{chat_session_id or 'nochat'}::job::{job_key}"


def _compute_occurrences(job_id: str | None, last_cursor_iso: str | None, now_dt: datetime) -> list[datetime]:
    """Enumerate logical fire times in (last_cursor, now].

    Recurring (cron) jobs persist in the jobstore, so the trigger is reachable
    and every missed occurrence is computed (QD: nothing collapsed silently).
    One-off jobs are removed from the store after firing, and the first-ever
    fire has no cursor — both yield a single occurrence. NEVER raises: a failed
    enumeration must not drop the fire, so it degrades to a single occurrence
    and logs (Rule 13 — nothing fails silently).
    """
    if not job_id or not last_cursor_iso:
        return [now_dt]
    try:
        from app.scheduler_instance import scheduler

        job = scheduler.get_job(job_id)
        trigger = getattr(job, "trigger", None) if job else None
        if trigger is None:
            return [now_dt]
        prev = datetime.fromisoformat(last_cursor_iso)
        occ: list[datetime] = []
        pointer = prev
        guard = 0
        while guard < _MAX_CATCHUP_OCCURRENCES + 5:
            nxt = trigger.get_next_fire_time(pointer, pointer)
            if nxt is None or nxt > now_dt:
                break
            occ.append(nxt)
            pointer = nxt
            guard += 1
        if not occ:
            return [now_dt]
        if len(occ) > _MAX_CATCHUP_OCCURRENCES:
            logger.critical(
                "Scheduled job %s: %d missed occurrences after downtime exceeds "
                "the %d cap — delivering the most recent %d and advancing the "
                "cursor past the rest.",
                job_id, len(occ), _MAX_CATCHUP_OCCURRENCES, _MAX_CATCHUP_OCCURRENCES,
            )
            occ = occ[-_MAX_CATCHUP_OCCURRENCES:]
        return occ
    except Exception:
        logger.warning(
            "Occurrence enumeration failed for job %s; single-occurrence "
            "fallback (the fire still happens, once).",
            job_id, exc_info=True,
        )
        return [now_dt]


def _occ_key(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


async def _drive_plan_to_completion(
    runner,
    user_id: str,
    session_id: str,
    first_response,
    actual_caller_id: str | None,
    task_id: str,
):
    """Re-invoke the runner while the plan still has pending steps.

    The ADK runner ends an invocation when the agent emits text without a
    trailing tool call. For enforced multi-step plans the LLM often summarizes
    after each step, ending the turn before the plan completes. This helper
    pumps the agent back in with a continuation prompt until the plan is done
    (or the iteration cap is hit).

    Returns the final AgentResponse.
    """
    from app.core.agent_executor import extract_agent_response
    from app.tools.planner import plan_has_pending_steps

    response = first_response
    iterations = 0
    # Short-circuit if the FIRST response already bailed (e.g. context limit
    # before any plan iteration) — the session is poisoned, looping wastes quota.
    if getattr(response, "error", None):
        logger.warning(
            "Task %s aborted before plan loop: %s", task_id, response.error,
        )
        return response
    while plan_has_pending_steps(session_id) and iterations < _MAX_PLAN_ITERATIONS:
        iterations += 1
        response = await extract_agent_response(
            runner, user_id, session_id, _PLAN_CONTINUATION_PROMPT,
            actual_caller_id=actual_caller_id,
        )
        # Terminal error → bail. Without this, a poisoned session (e.g. over
        # the 1M-token input cap) gets retried 25× against the same poisoned
        # state, each iteration burning rate-limit quota until 429s start
        # firing. Documented incident: 2026-04-30 FBA-discrepancy task hit
        # context limit, looped 25× and exhausted the paid-tier-2 input quota.
        if getattr(response, "error", None):
            logger.warning(
                "Task %s plan loop aborted at iteration %d: %s",
                task_id, iterations, response.error,
            )
            return response
    if iterations >= _MAX_PLAN_ITERATIONS and plan_has_pending_steps(session_id):
        logger.warning(
            "Task %s hit plan-iteration cap (%d); plan still has pending steps.",
            task_id, _MAX_PLAN_ITERATIONS,
        )
    return response


async def run_scheduled_task(
    task_prompt: str,
    notify: dict,
    owner_user_id: str,
    task_id: str = None,
    steps: list[str] = None,
    job_id: str = None,
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
    before the agent runs — `plan_enforcer` injects it from turn one, so the
    LLM cannot skip or paraphrase steps. If `steps` is None, the task runs
    under normal (LLM-decided) flow.
    """
    from app.core.agent_executor import extract_agent_response, update_session_state
    from run_bot import get_runner
    import uuid

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
    # Stamp owner_user_id on every event so the log can be filtered
    # by ownership at read time (get_scheduled_task_logs gate).
    _log_job_event(
        "fire_start",
        task_id=task_id,
        kind="scheduled",
        owner_user_id=owner_user_id or "",
        prompt_preview=task_prompt[:140],
        channel=(notify or {}).get("chat_id") or (notify or {}).get("channel"),
    )

    runner = get_runner()

    # Set inside the durable run path once the occurrence(s) for this fire are
    # known; consumed AFTER delivery so the cursor only advances on a real
    # send (reviewer 🟡 — never mark an occurrence delivered before it is).
    cursor_advance = None
    # True ONLY when the agent produced a genuine result this fire (did not
    # raise, not empty, not a guardrail block, no terminal AgentResponse
    # error). A delivered failure-NOTICE is NOT a delivered occurrence: the
    # cursor advance is gated on (delivered_ok AND agent_ok) so a transient
    # agent failure on a RECURRING job re-attempts next wake instead of
    # consuming the occurrence (reviewer 🟡 round 2 — restores the
    # at-least-once-on-agent-failure property of the first slice).
    agent_ok = False

    if runner:
        # Slice-1: run the scheduled turn in a DURABLE side-session LINKED to
        # the creating chat, through the SAME boot Runner (app=ori_app) — so
        # the turn resolves under the SAME ADK app_name the live chat uses.
        # D5: a divergent app_name silently writes an orphan session row and
        # the in-chat follow-up breaks with NO error. The boot Runner makes
        # this correct by construction (it IS Runner(app=ori_app)), so Slice-1
        # needs no per-fire App — that arrives with the LATER per-job
        # SequentialAgent slice. owner_user_id is required; a missing value
        # means a stale pre-contract job.
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
            _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id="", error="missing owner_user_id")
            await _deliver_with_fallback(notify, response, task_id=task_id)
            return

        job_key = _stable_job_key(job_id, owner_user_id, notify, task_prompt)
        chat_session_id = _chat_identity_from_notify(notify)
        if not chat_session_id:
            logger.warning(
                "Scheduled task %s (job %s): no origin_session_id on notify — "
                "running in an isolated durable side-session (legacy notify "
                "dict; in-chat follow-up still routes via the delivery target).",
                task_id, job_key,
            )
        session_id = _linked_side_session_id(chat_session_id, job_key)
        # Side-session lives UNDER the chat's user_id (== chat session_id for
        # chats). Owner identity is still promoted into state via
        # actual_caller_id so per-user tools (Google, prefs, admin) work.
        user_id = chat_session_id or "system_scheduler"

        try:
            # DURABLE: get-or-create, NEVER delete. The cursor in
            # session.state must survive across fires. The per-fire
            # plan/scratchpad sidecar is still reset so a steps task starts
            # each fire from its frozen seeded plan (Slice-1 keeps current
            # per-fire step behaviour; cross-run plan persistence is the LATER
            # fidelity slice). _cleanup_ephemeral_task_state only removes the
            # tmp/plans file + scratchpad keyed by session_id — it does NOT
            # touch ADK session.state, so the durable cursor is unaffected.
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
            if session is None:
                session = await runner.session_service.create_session(
                    app_name=runner.app_name, user_id=user_id, session_id=session_id
                )

            state = dict(session.state) if session and session.state else {}
            cursor_key = f"job:{job_key}:cursor"
            delivered_key = f"job:{job_key}:delivered_occurrences"
            last_cursor = state.get(cursor_key)
            delivered = list(state.get(delivered_key) or [])

            now_dt = datetime.now(timezone.utc)
            occurrences = _compute_occurrences(job_id, last_cursor, now_dt)
            occ_keys = [_occ_key(o) for o in occurrences]
            new_occ_keys = [k for k in occ_keys if k not in delivered]

            if not new_occ_keys:
                # Every occurrence this wake represents was already delivered:
                # an APScheduler replay or a process restart re-firing the same
                # logical occurrence must NOT double-send (QD: never-double-send).
                logger.info(
                    "Scheduled task %s (job %s): all %d occurrence(s) already "
                    "delivered — idempotent skip, no double-send.",
                    task_id, job_key, len(occ_keys),
                )
                ACTIVE_TASKS[task_id]["status"] = "Skipped (already delivered)"
                ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
                _log_job_event(
                    "fire_end", task_id=task_id, kind="scheduled",
                    owner_user_id=owner_user_id or "",
                    status="skipped_already_delivered", job_id=job_key,
                )
                return

            # Capture the occurrence context now (the agent may raise below).
            # The cursor is advanced + occurrences marked delivered only AFTER
            # _deliver_with_fallback confirms the send (reviewer 🟡): on a
            # recoverable channel failure a RECURRING job must re-attempt this
            # occurrence next wake, not silently lose it.
            cursor_advance = {
                "user_id": user_id,
                "session_id": session_id,
                "cursor_key": cursor_key,
                "delivered_key": delivered_key,
                "prev_delivered": list(delivered),
                "new_occ_keys": list(new_occ_keys),
                "new_cursor": max(occ_keys),
            }

            query = (
                f"Scheduled Task: {task_prompt}\n"
                "(This is an automated reminder. Execute the task or deliver the reminder to the user. "
                "Do not ask for missing credentials; stop gracefully if something is missing.)"
            )
            if len(new_occ_keys) > 1:
                # Coalesced catch-up after downtime: ONE consolidated message
                # for every missed occurrence (QD), never N separate sends.
                query += (
                    f"\n\n[CATCH-UP] The assistant was offline; this run consolidates "
                    f"{len(new_occ_keys)} missed occurrences ({', '.join(new_occ_keys)}). "
                    "Produce ONE consolidated result covering all of them — do not "
                    "emit separate messages per occurrence."
                )

            # Per-fire plan/scratchpad reset (keeps current steps behaviour;
            # does NOT touch the durable session.state cursor).
            _cleanup_ephemeral_task_state(session_id)
            if steps:
                from app.tools.planner import seed_plan
                seed_plan(session_id, task_prompt[:5000], steps)
                logger.info(
                    "Scheduled task %s: seeded enforced plan with %d step(s)",
                    task_id, len(steps),
                )

            response = await extract_agent_response(
                runner, user_id, session_id, query,
                actual_caller_id=owner_user_id or None,
            )
            if steps:
                response = await _drive_plan_to_completion(
                    runner, user_id, session_id, response,
                    actual_caller_id=owner_user_id or None,
                    task_id=task_id,
                )
            # Inspect the AgentResponse BEFORE stringifying: .error is the
            # terminal-failure signal (rate limit / context limit / readonly /
            # max-retries) — the text is then a user-facing error notice, NOT
            # the task result, so it must NOT count as a delivered occurrence.
            resp_err = getattr(response, "error", None)
            response = response.text if hasattr(response, "text") else str(response)
            if not response or not response.strip():
                # Agent returned empty — treat as failure so user sees something.
                response = (
                    f":warning: Scheduled task `{task_id}` produced an empty response.\n"
                    f"Prompt: {task_prompt[:200]}"
                )
                ACTIVE_TASKS[task_id]["status"] = "Failed (empty response)"
                _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id=owner_user_id or "", error="empty response")
            elif "Guardrail Intervention:" in response:
                response = f":warning: Scheduled task `{task_id}` hit a guardrail.\n{response}"
                ACTIVE_TASKS[task_id]["status"] = "Failed (guardrail)"
                _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id=owner_user_id or "", error="guardrail intervention")
            elif resp_err:
                # Terminal agent error — the notice text is already user-facing
                # (set by extract_agent_response). Deliver it, but do NOT mark
                # the occurrence delivered (agent_ok stays False).
                ACTIVE_TASKS[task_id]["status"] = f"Failed (agent error: {resp_err})"
                _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id=owner_user_id or "", error=f"agent error: {resp_err}")
            else:
                ACTIVE_TASKS[task_id]["status"] = "Completed"
                agent_ok = True  # genuine result this fire
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
            _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id=owner_user_id or "", error=f"{type(e).__name__}: {e}")
    else:
        # Runner unavailable — bot is starting up or shutting down. Report honestly.
        response = (
            f":x: Scheduled task `{task_id}` could not run: agent runner unavailable.\n"
            f"Prompt: {task_prompt[:200]}"
        )
        ACTIVE_TASKS[task_id]["status"] = "Failed (no runner)"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        _log_job_event("error", task_id=task_id, kind="scheduled", owner_user_id=owner_user_id or "", error="runner unavailable")

    # Deliver to the user's channel, with fallback to origin on failure.
    delivered_ok = await _deliver_with_fallback(notify, response, task_id=task_id)

    # GATE the durable cursor advance on actual delivery (reviewer 🟡): mark
    # the occurrence(s) delivered + advance the cursor ONLY when the channel
    # accepted the message. On a recoverable send failure we deliberately do
    # NOT advance — a RECURRING job then re-attempts this occurrence next
    # wake instead of silently dropping a reminder inside the deliver-to-
    # target seam Slice-1 exists to make reliable. A one-off has no next
    # wake, so it is logged-not-retried (unchanged; no retry vehicle without
    # re-arming the trigger, which is out of Slice-1 scope).
    if cursor_advance is not None:
        if delivered_ok and agent_ok:
            try:
                prev = cursor_advance["prev_delivered"]
                merged = prev + [
                    k for k in cursor_advance["new_occ_keys"] if k not in prev
                ]
                if len(merged) > _MAX_DELIVERED_TRACKED:
                    merged = merged[-_MAX_DELIVERED_TRACKED:]
                await update_session_state(
                    runner,
                    cursor_advance["user_id"],
                    cursor_advance["session_id"],
                    {
                        cursor_advance["cursor_key"]: cursor_advance["new_cursor"],
                        cursor_advance["delivered_key"]: merged,
                    },
                )
            except Exception:
                logger.error(
                    "Scheduled task %s (job %s): FAILED to advance durable "
                    "cursor after delivery — next wake may re-deliver %s.",
                    task_id, job_key, cursor_advance["new_occ_keys"],
                    exc_info=True,
                )
        elif delivered_ok and not agent_ok:
            # Channel was healthy and a failure NOTICE was delivered, but the
            # agent did not produce a genuine result (raised / empty /
            # guardrail / terminal error). The occurrence is NOT consumed: a
            # RECURRING job re-attempts it next wake (restores the
            # at-least-once-on-agent-failure property; a one-off has no retry
            # vehicle so it is logged-not-retried). Rule 13: named, not silent.
            logger.error(
                "Scheduled task %s (job %s): agent FAILED for occurrence(s) "
                "%s (failure notice delivered) — cursor NOT advanced; a "
                "recurring job re-attempts next wake (a one-off has no retry "
                "vehicle).",
                task_id, job_key, cursor_advance["new_occ_keys"],
            )
        else:
            logger.error(
                "Scheduled task %s (job %s): delivery FAILED for occurrence(s) "
                "%s — cursor NOT advanced; a recurring job re-attempts next "
                "wake (a one-off has no retry vehicle).",
                task_id, job_key, cursor_advance["new_occ_keys"],
            )
    duration_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
    _log_job_event(
        "fire_end",
        task_id=task_id,
        kind="scheduled",
        owner_user_id=owner_user_id or "",
        status=ACTIVE_TASKS[task_id]["status"],
        duration_ms=duration_ms,
        response_preview=(response or "")[:200],
    )


async def run_system_task(
    task_prompt: str,
    notify: dict,
    admin_user_id: str,
    silent: bool = False,
    task_id: str = None,
    steps: list[str] = None,
):
    """
    Executed by APScheduler for admin-only system maintenance tasks.
    Runs the agent with full privileges in an isolated session, then cleans up.

    If `steps` is provided (enforced task), the plan is seeded into storage
    before the agent runs — plan_enforcer injects it from turn one. LLM cannot
    skip or paraphrase steps. If `steps` is None, normal LLM-decided flow.
    """
    import uuid

    from app.core.agent_executor import extract_agent_response
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
        try:
            await runner.session_service.delete_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
        except Exception:
            pass
        _cleanup_ephemeral_task_state(session_id)

        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        # Enforced task: seed the plan before first turn. See run_scheduled_task
        # for the same pattern.
        if steps:
            from app.tools.planner import seed_plan
            seed_plan(session_id, task_prompt[:500], steps)
            logger.info(
                "System task %s: seeded enforced plan with %d step(s)",
                task_id, len(steps),
            )

        # Pass admin identity via actual_caller_id so state_setter picks it up
        logger.info("System Task: Executing agent for %s", task_id)
        response = await extract_agent_response(
            runner, user_id, session_id, query, actual_caller_id=admin_user_id
        )
        if steps:
            response = await _drive_plan_to_completion(
                runner, user_id, session_id, response,
                actual_caller_id=admin_user_id,
                task_id=task_id,
            )
        response = response.text if hasattr(response, "text") else str(response)

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
        _cleanup_ephemeral_task_state(session_id)
        duration_ms = int((datetime.now() - start_ts).total_seconds() * 1000)
        _log_job_event(
            "fire_end",
            task_id=task_id,
            kind="system",
            status=ACTIVE_TASKS[task_id].get("status", "unknown"),
            duration_ms=duration_ms,
        )


async def _deliver_message(
    notify: dict,
    message: str,
    task_id: str = "",
    session_message: str | None = None,
    file_path: str | None = None,
) -> bool:
    """Send a message to the user's chat AND inject it into their session history.

    If file_path is provided and exists, it is uploaded as media with the message
    as a caption.

    Returns True on successful send, False on any failure (no adapter, adapter raised,
    channel not delivered). Failures are logged to the scheduler job log so the user
    can diagnose them via get_scheduled_task_logs.

    The injection is what makes scheduled task output visible to the agent on the
    user's next turn.
    """
    from app.core.transport import get_adapter

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
        if file_path and os.path.isfile(file_path):
            import asyncio as _asyncio
            import mimetypes
            mime, _ = mimetypes.guess_type(file_path)
            # File reads happen on the asyncio event-loop thread. A
            # multi-MB media payload (image, audio, PDF) read with a
            # plain ``open().read()`` blocks the loop for the full
            # duration of the read, stalling every other coroutine
            # (Slack/Telegram polling, contract fires, A2A traffic).
            # Push the read to the default thread executor so the
            # loop stays responsive.
            data = await _asyncio.to_thread(_read_bytes, file_path)
            # Most adapters (Slack, Telegram) support 'caption' on send_media.
            await adapter.send_media(
                target, data, mime or "application/octet-stream", caption=message
            )
        else:
            await adapter.send_message(target, message)
        delivered = True
    except Exception as e:
        logger.error("Failed to deliver message via adapter: %s", e)
        _log_job_event("delivery_failure", task_id=task_id, reason=str(e), target=target)
        delivered = False

    # Mirror the delivered message into the chat's session so the model has it
    # in conversation history when the user asks a follow-up.
    await _inject_into_session(notify, session_message if session_message is not None else message)
    return delivered


async def _deliver_with_fallback(notify: dict, message: str, task_id: str, file_path: str | None = None) -> bool:
    """Deliver to notify's channel; if that fails AND the creator's
    session is a different channel, route a failure notice to the
    creator so they're never left in the dark.

    Reads ``origin_session_id`` for the creator (always the
    scheduling session) and falls back to the legacy alias for
    pre-2026-05-14 notify dicts. ``target_session_id`` is the
    intended primary destination; same-channel checks compare
    against IT.

    Returns True iff the message reached the user (primary channel OR
    the fallback creator session). The scheduled fire path gates its
    durable cursor advance on this — a recoverable send failure must
    NOT mark the occurrence delivered (reviewer 🟡).
    """
    delivered = await _deliver_message(notify, message, task_id=task_id, file_path=file_path)
    if delivered:
        return True

    # Fallback — ALWAYS the creator's session. The 2026-05-13 audit
    # found that legacy ``_stamp_ownership`` clobbered
    # ``origin_session_id`` with ``deliver_to`` whenever ``deliver_to``
    # was set, so the fallback retried delivery to the same broken
    # target. ``origin_session_id`` now keeps the creator's session
    # verbatim.
    origin = (notify or {}).get("origin_session_id", "")
    primary = (notify or {}).get("chat_id") or (notify or {}).get("channel", "")
    if not origin or not primary:
        return False
    # Skip the fallback if origin == primary channel (no point in
    # retrying the same destination).
    if (
        origin == f"sl_{primary}"
        or origin == f"tg_{primary}"
        or origin.endswith(f"_{primary}")
    ):
        return False

    from app.core.transport import parse_notify_from_session_id
    fallback_notify = parse_notify_from_session_id(origin)
    if not fallback_notify:
        return False

    fallback_msg = (
        f":warning: Could not deliver scheduled task `{task_id}` to its target channel. "
        f"Routing to the session that scheduled it.\n\n{message}"
    )
    return await _deliver_message(fallback_notify, fallback_msg, task_id=task_id, file_path=file_path)


async def _inject_into_session(notify: dict, message: str):
    """Append the delivered scheduled-task text as a model-authored event in the
    DELIVERY channel's session, so it shows up in conversation history on the
    next turn in that channel.

    Reads ``target_session_id`` first (the channel that just received the
    delivery). Falls back to ``origin_session_id`` for legacy notify dicts
    that pre-date the split, since pre-2026-05-14 ``_stamp_ownership``
    wrote the target into ``origin_session_id``.

    Silently no-ops if the session doesn't exist or the runner isn't
    available.
    """
    target_session = (
        (notify or {}).get("target_session_id")
        or (notify or {}).get("origin_session_id")
    )
    if not target_session:
        return

    from run_bot import get_runner

    runner = get_runner()
    if not runner:
        return

    try:
        from google.adk.events.event import Event
        from google.genai import types as _types
        import uuid as _uuid
        import time as _time

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
