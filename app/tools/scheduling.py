import os
import uuid
from datetime import datetime

from google.adk.tools.tool_context import ToolContext


def get_current_time(timezone: str, tool_context: ToolContext) -> dict:
    """Returns the current date and time in the specified timezone.

    ALWAYS call this tool before scheduling any reminder or task, so you know the
    current time and can calculate the correct target time.

    Args:
        timezone (str): IANA timezone name (e.g., 'Europe/Kyiv', 'America/New_York', 'UTC').

    Returns:
        dict: Current date, time, and timezone info.
    """
    tz = _parse_tz(timezone)
    if tz is None:
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'. Use IANA format like 'Europe/Kyiv', 'America/New_York', 'UTC'."}

    now = datetime.now(tz)
    return {
        "status": "success",
        "datetime": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": timezone,
        "weekday": now.strftime("%A"),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOW_NAMES = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def _validate_cron_dow(cron_expression: str) -> str | None:
    """Reject numeric day-of-week fields. APScheduler's from_crontab uses
    0=Mon / 6=Sun (non-standard), so numeric DOW reliably produces off-by-one
    bugs when the agent assumes Vixie cron (1=Mon). Force explicit 3-letter
    names (MON,WED,FRI).

    Returns an error string on invalid input, None when valid.
    """
    parts = cron_expression.strip().split()
    if len(parts) != 5:
        return None  # let from_crontab surface the real error
    dow = parts[4]
    if dow in {"*", "?"}:
        return None
    tokens = dow.replace(",", " ").replace("-", " ").replace("/", " ").split()
    for tok in tokens:
        if tok.isdigit():
            return (
                f"Ambiguous day-of-week '{dow}'. Use 3-letter names (MON,TUE,WED,THU,FRI,SAT,SUN) — "
                f"numeric DOW is rejected because APScheduler's from_crontab uses 0=Mon (non-standard) "
                f"and produces off-by-one bugs. Example: '0 17 * * MON,WED,FRI'."
            )
        if tok.upper() not in _DOW_NAMES:
            return f"Unknown day-of-week token '{tok}'. Use MON,TUE,WED,THU,FRI,SAT,SUN."
    return None


def _parse_tz(timezone: str):
    """Return a ZoneInfo object or None on failure."""
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(timezone)
    except Exception:
        return None


def _parse_iso_to_tz(iso_str: str, tz) -> datetime:
    """Parse an ISO 8601 datetime.

    - Naive input → interpret in `tz`.
    - TZ-aware input → convert to `tz` (preserves the absolute moment).

    Raises ValueError on malformed input.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _get_user_id(tool_context: ToolContext) -> str:
    """Read user_id from session state; '' if unknown."""
    session = getattr(tool_context, "session", None)
    if not session:
        return ""
    state = session.state if hasattr(session, "state") else {}
    if isinstance(state, dict):
        return state.get("user_id", "") or ""
    getter = getattr(state, "get", None)
    return (getter("user_id", "") or "") if callable(getter) else ""


def _get_session_id(tool_context: ToolContext) -> str:
    """Read the current chat session_id from tool_context, '' if unknown."""
    session = getattr(tool_context, "session", None)
    if not session:
        return ""
    return getattr(session, "session_id", None) or getattr(session, "id", None) or ""


def _get_session_notify_info(tool_context: ToolContext) -> dict:
    """Extract notification info (channel type + id) from the current session via the adapter registry."""
    from app.runtime.transport import parse_notify_from_session_id

    sid = _get_session_id(tool_context)
    if not sid:
        return {}
    return parse_notify_from_session_id(sid) or {}


def _resolve_notify(tool_context: ToolContext, deliver_to: str = "") -> dict:
    """Resolve notification target — use deliver_to if provided, otherwise fall back to current session."""
    if deliver_to:
        from app.runtime.transport import parse_notify_from_session_id
        notify = parse_notify_from_session_id(deliver_to)
        if notify:
            return notify
    return _get_session_notify_info(tool_context)


def _require_admin(tool_context: ToolContext) -> str | None:
    """Check if the current user is an admin. Returns the admin user_id if authorized, None otherwise."""
    user_id = _get_user_id(tool_context)
    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]
    if admin_users and user_id in admin_users:
        return user_id
    return None


def _is_admin(tool_context: ToolContext) -> bool:
    return _require_admin(tool_context) is not None


def _job_owner(job) -> str:
    """Return the user_id who owns a job, or '' for legacy jobs with no recorded owner."""
    try:
        kwargs = job.kwargs or {}
    except Exception:
        return ""
    # New contract: owner_user_id is an explicit top-level kwarg for user tasks,
    # admin_user_id for system tasks. Both are set at schedule creation time and
    # persisted in APScheduler's jobstore.
    owner = kwargs.get("owner_user_id", "") or kwargs.get("admin_user_id", "") or ""
    return owner


def _can_access_job(job, user_id: str, is_admin: bool) -> bool:
    """Admins touch everything. Non-admins only touch their own non-system jobs.
    Legacy jobs with no recorded owner are admin-only.
    """
    if is_admin:
        return True
    if job.id.startswith("sys_"):
        return False
    owner = _job_owner(job)
    if not owner:
        return False
    return owner == user_id


def _validate_steps(steps):
    """Validate the optional `steps` kwarg accepted by scheduling tools.

    Returns:
        - None if steps is None / empty (non-enforced task).
        - A cleaned list of stripped non-empty step strings if valid.
        - A dict `{"status": "error", "message": ...}` if the shape is invalid,
          so the caller can short-circuit by returning it to the user.
    """
    if steps is None:
        return None
    if not isinstance(steps, list):
        return {
            "status": "error",
            "message": "`steps` must be a list of strings (one entry per checklist step). "
                       "Omit it entirely for non-enforced tasks.",
        }
    cleaned = [s.strip() for s in steps if isinstance(s, str) and s.strip()]
    if len(cleaned) != len([s for s in steps if s is not None]):
        return {
            "status": "error",
            "message": "`steps` contains entries that are not non-empty strings. "
                       "Each step must be a single descriptive string.",
        }
    if not cleaned:
        # Explicit empty list = same as None
        return None
    return cleaned


def _stamp_ownership(notify: dict, user_id: str, deliver_to: str, origin_session_id: str = "") -> dict:
    """Attach delivery + history-injection metadata to the notify dict.

    Ownership is NOT stored here any more — owner_user_id is an explicit top-level
    kwarg on the job itself (see schedule_one_off_task / schedule_recurring_task).
    This function only records where to deliver results and which chat session's
    history should receive the synthetic event on fire. The `user_id` arg is
    retained for signature compatibility but unused; it will be removed once
    all callers are updated.
    """
    del user_id  # intentionally ignored — see docstring
    stamped = dict(notify) if notify else {}
    if deliver_to:
        stamped["deliver_to_session"] = deliver_to
    # Prefer an explicit deliver_to target; fall back to the origin session.
    target_session = deliver_to or origin_session_id
    if target_session:
        stamped["origin_session_id"] = target_session
    return stamped


# ---------------------------------------------------------------------------
# User scheduling tools
# ---------------------------------------------------------------------------

def schedule_one_off_task(
    task_prompt: str, run_at_iso_datetime: str, timezone: str, tool_context: ToolContext,
    deliver_to: str = "",
    steps: list[str] | None = None,
) -> dict:
    """Schedules the agent to execute a specific task once at a specific date and time.

    When the scheduled time arrives, the agent will fully process the task_prompt —
    generating content, running tools, or fetching data as needed — and deliver the
    result to the destination channel. Write the prompt as an instruction for your future self.

    Use this tool when the user asks to "remind me", "check this tomorrow", or "do X at Y time".
    IMPORTANT: Always call get_current_time first to know the current time before scheduling.

    Args:
        task_prompt (str): The instruction the agent should execute when the time comes (e.g. 'Tell the user a funny joke to start their morning' or 'Check Keepa for ASIN B08X and report the price').
        run_at_iso_datetime (str): The date and time to run the task, in ISO 8601 format (e.g., '2026-03-25T10:00:00'). Interpreted in the provided timezone if naive; honored as-is if it already carries an offset.
        timezone (str): IANA timezone for the scheduled time (e.g., 'Europe/Kyiv', 'UTC').
        deliver_to (str): Optional session ID to deliver results to instead of the current chat. Use this to post to a different platform or channel (e.g. 'sl_C01234ABC' for a Slack channel, 'tg_123456' for a Telegram chat). If empty, delivers to the current chat.
        steps (list[str]): Optional ordered checklist. If provided, the plan is seeded into
            storage before the agent's first turn and the plan_executor workflow drives it step-by-step —
            the LLM cannot skip or paraphrase. Use for tasks that must follow an exact
            sequence on every fire. See scheduling-skill → Enforced step-by-step scheduling.

    Returns:
        dict: Status of the scheduling operation.
    """
    from app.scheduler_instance import scheduler
    from app.tasks import run_scheduled_task

    tz = _parse_tz(timezone)
    if tz is None:
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        run_date = _parse_iso_to_tz(run_at_iso_datetime, tz)
    except (ValueError, TypeError):
        return {"status": "error", "message": "Invalid datetime format. Must be ISO 8601."}

    if run_date <= datetime.now(tz):
        return {"status": "error", "message": "Scheduled time is in the past."}

    notify = _resolve_notify(tool_context, deliver_to)
    if not notify:
        return {
            "status": "error",
            "message": "Cannot schedule: no delivery target could be resolved. "
                       "Provide a valid `deliver_to` session ID, or schedule from a chat that has a registered transport.",
        }

    owner_user_id = _get_user_id(tool_context)
    if not owner_user_id:
        return {
            "status": "error",
            "message": "Cannot schedule: current user identity could not be determined from session state. "
                       "This task has no one to run as. Please retry from an authenticated chat.",
        }

    validated_steps = _validate_steps(steps)
    if isinstance(validated_steps, dict):  # error shape
        return validated_steps

    notify = _stamp_ownership(notify, owner_user_id, deliver_to, _get_session_id(tool_context))

    job_id = f"oneoff_{uuid.uuid4().hex[:8]}"
    job_kwargs = {
        "task_prompt": task_prompt,
        "notify": notify,
        "owner_user_id": owner_user_id,
    }
    if validated_steps:
        job_kwargs["steps"] = validated_steps
    job = scheduler.add_job(
        run_scheduled_task,
        "date",
        run_date=run_date,
        kwargs=job_kwargs,
        id=job_id,
    )

    dest = deliver_to or "current chat"
    now_local = datetime.now(tz)
    next_run = str(getattr(job, "next_run_time", run_date))
    return {
        "status": "success",
        "job_id": job_id,
        "now": now_local.isoformat(timespec="seconds"),
        "next_run": next_run,
        "delivers_to": dest,
        "message": (
            f"Scheduled {job_id}. now={now_local.isoformat(timespec='seconds')}. "
            f"next_run={next_run}. delivers_to={dest}. Quote 'next_run' verbatim — do not translate."
        ),
    }


def schedule_recurring_task(
    task_prompt: str, cron_expression: str, timezone: str, tool_context: ToolContext,
    deliver_to: str = "",
    steps: list[str] | None = None,
) -> dict:
    """Schedules the agent to execute a task automatically on a recurring schedule.

    When the scheduled time arrives, the agent will fully process the task_prompt —
    generating content, running tools, or fetching data as needed — and deliver the
    result to the destination channel. Write the prompt as an instruction for your future self.

    Use this tool when the user asks to "regularly monitor", "do X every day", or "check X every Monday".
    IMPORTANT: Always call get_current_time first to confirm the user's timezone.

    Args:
        task_prompt (str): The instruction the agent should execute (e.g. 'Perform a management check for ASIN B08X').
        cron_expression (str): A standard 5-part cron expression defining the schedule (e.g., '0 10 * * *' for every day at 10 AM).
        timezone (str): IANA timezone for the cron schedule (e.g., 'Europe/Kyiv', 'UTC').
        deliver_to (str): Optional session ID to deliver results to instead of the current chat. Use this to post to a different platform or channel (e.g. 'sl_C01234ABC' for a Slack channel, 'tg_123456' for a Telegram chat). If empty, delivers to the current chat.
        steps (list[str]): Optional ordered checklist. If provided, the plan is seeded into
            storage before the agent's first turn and the plan_executor workflow drives it step-by-step —
            the LLM cannot skip or paraphrase. Use for tasks that must follow an exact
            sequence on every fire. See scheduling-skill → Enforced step-by-step scheduling.

    Returns:
        dict: Status of the scheduling operation.
    """
    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler_instance import scheduler
    from app.tasks import run_scheduled_task

    tz = _parse_tz(timezone)
    if tz is None:
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    dow_err = _validate_cron_dow(cron_expression)
    if dow_err:
        return {"status": "error", "message": dow_err}

    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid cron expression."}

    notify = _resolve_notify(tool_context, deliver_to)
    if not notify:
        return {
            "status": "error",
            "message": "Cannot schedule: no delivery target could be resolved. "
                       "Provide a valid `deliver_to` session ID, or schedule from a chat that has a registered transport.",
        }

    owner_user_id = _get_user_id(tool_context)
    if not owner_user_id:
        return {
            "status": "error",
            "message": "Cannot schedule: current user identity could not be determined from session state. "
                       "This task has no one to run as. Please retry from an authenticated chat.",
        }

    validated_steps = _validate_steps(steps)
    if isinstance(validated_steps, dict):
        return validated_steps

    notify = _stamp_ownership(notify, owner_user_id, deliver_to, _get_session_id(tool_context))

    job_id = f"cron_{uuid.uuid4().hex[:8]}"
    job_kwargs = {
        "task_prompt": task_prompt,
        "notify": notify,
        "owner_user_id": owner_user_id,
    }
    if validated_steps:
        job_kwargs["steps"] = validated_steps
    job = scheduler.add_job(
        run_scheduled_task,
        trigger=trigger,
        kwargs=job_kwargs,
        id=job_id,
    )

    dest = deliver_to or "current chat"
    now_local = datetime.now(tz)
    next_run = str(getattr(job, "next_run_time", "unknown"))
    return {
        "status": "success",
        "job_id": job_id,
        "cron": cron_expression,
        "timezone": timezone,
        "now": now_local.isoformat(timespec="seconds"),
        "next_run": next_run,
        "delivers_to": dest,
        "message": (
            f"Scheduled {job_id} with cron '{cron_expression}' ({timezone}). "
            f"now={now_local.isoformat(timespec='seconds')}. next_run={next_run}. "
            f"delivers_to={dest}. Quote 'next_run' verbatim — do not translate or guess."
        ),
    }


def list_scheduled_tasks(tool_context: ToolContext) -> dict:
    """Lists the current user's scheduled tasks/reminders.

    Admins see all tasks (including system tasks). Non-admins see only their own.

    Use this when the user asks to see their reminders, scheduled tasks, or wants to know what's coming up.

    Returns:
        dict: List of scheduled tasks with their details.
    """
    from app.scheduler_instance import scheduler

    user_id = _get_user_id(tool_context)
    is_admin = _is_admin(tool_context)

    jobs = scheduler.get_jobs()
    tasks = []
    for job in jobs:
        if not _can_access_job(job, user_id, is_admin):
            continue
        kwargs = job.kwargs or {}
        task_info = {
            "job_id": job.id,
            "task": kwargs.get("task_prompt", "Unknown"),
            "next_run": str(job.next_run_time) if job.next_run_time else "N/A",
            "type": (
                "system (recurring)" if job.id.startswith("sys_cron_")
                else "system (one-off)" if job.id.startswith("sys_oneoff_")
                else "recurring" if job.id.startswith("cron_")
                else "one-off"
            ),
            "owner": _job_owner(job) or "legacy",
        }
        tasks.append(task_info)

    if not tasks:
        return {"status": "success", "tasks": [], "message": "No scheduled tasks."}
    return {"status": "success", "tasks": tasks}


def get_scheduled_task_logs(
    tool_context: ToolContext, task_id: str = "", limit: int = 50,
) -> dict:
    """Returns recent fire events from the scheduler job log (JSONL).

    Each event records one of: fire_start, fire_end, error. Events include
    timestamps, task_id, kind (scheduled/system), duration_ms, status, and
    response_preview. Use this to check whether a scheduled job actually ran,
    how long it took, and what it produced — without re-running it.

    Args:
        task_id (str): Optional task_id to filter by (e.g. 'sched_a1b2c3d4'). Empty = all events.
        limit (int): Maximum number of events to return (most recent first). Default 50.

    Returns:
        dict: {'status', 'events': [...]} — events are chronological (newest last).
    """
    import json as _json
    import os as _os

    path = _os.path.abspath("./data/scheduler_jobs.log")
    if not _os.path.exists(path):
        return {"status": "success", "events": [], "message": "No scheduler log yet — no jobs have fired."}

    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return {"status": "error", "message": f"Failed to read log: {e}"}

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
        except Exception:
            continue
        if task_id and rec.get("task_id") != task_id:
            continue
        events.append(rec)

    events = events[-max(1, int(limit)):]
    return {"status": "success", "events": events, "count": len(events)}


def delete_scheduled_task(job_id: str, tool_context: ToolContext) -> dict:
    """Deletes a scheduled task/reminder.

    Only the owner of a task (or an admin) may delete it.
    System tasks (`sys_*`) are admin-only.

    Use this when the user wants to cancel a reminder or stop a recurring task.
    Call list_scheduled_tasks first to get the job_id.

    Args:
        job_id (str): The job ID to delete (e.g., 'oneoff_a1b2c3d4' or 'cron_e5f6g7h8').

    Returns:
        dict: Status of the deletion.
    """
    from app.scheduler_instance import scheduler

    job = scheduler.get_job(job_id)
    if job is None:
        return {"status": "error", "message": f"Task {job_id} not found."}

    if not _can_access_job(job, _get_user_id(tool_context), _is_admin(tool_context)):
        return {"status": "error", "message": f"Access denied: task {job_id} is not yours."}

    try:
        scheduler.remove_job(job_id)
    except Exception as e:
        return {"status": "error", "message": f"Failed to delete {job_id}: {e}"}
    return {"status": "success", "message": f"Deleted task {job_id}."}


def edit_scheduled_task(
    job_id: str, tool_context: ToolContext,
    new_task_prompt: str = "",
    new_run_at_iso_datetime: str = "",
    new_cron_expression: str = "",
    timezone: str = "",
    new_steps: list[str] | None = None,
    clear_steps: bool = False,
) -> dict:
    """Edits an existing scheduled task — all fields are optional, pass only what you want to change.

    - `new_task_prompt` changes the instruction (works for both one-off and recurring).
    - For one-off jobs: `new_run_at_iso_datetime` + `timezone` changes the run time.
    - For recurring jobs: `new_cron_expression` + `timezone` changes the schedule.
    - `new_steps` replaces the enforced step list (adds enforcement to a non-enforced task,
      or updates the checklist of an already-enforced one).
    - `clear_steps=True` removes enforcement (task reverts to normal LLM-decided flow).
    - Cannot convert a one-off to recurring (or vice versa) — delete and recreate instead.
    - Ownership, delivery destination, and admin status are preserved unchanged.

    Only the owner of a task (or an admin) may edit it. Call list_scheduled_tasks first to get the job_id.

    Args:
        job_id (str): The job ID to edit.
        new_task_prompt (str): Optional. New task instruction. Empty = keep existing.
        new_run_at_iso_datetime (str): Optional. New ISO 8601 datetime for one-off jobs. Empty = keep existing.
        new_cron_expression (str): Optional. New 5-part cron expression for recurring jobs. Empty = keep existing.
        timezone (str): IANA timezone — required if new_run_at_iso_datetime or new_cron_expression is provided.
        new_steps (list[str]): Optional. Replace the enforced step list with this. Provide ordered
            step descriptions. Ignored if clear_steps=True.
        clear_steps (bool): If True, removes the enforced step list from the task (future fires
            run under normal LLM-decided flow). Mutually exclusive with new_steps.

    Returns:
        dict: Status of the edit.
    """
    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler_instance import scheduler

    job = scheduler.get_job(job_id)
    if job is None:
        return {"status": "error", "message": f"Task {job_id} not found."}

    if not _can_access_job(job, _get_user_id(tool_context), _is_admin(tool_context)):
        return {"status": "error", "message": f"Access denied: task {job_id} is not yours."}

    if not (new_task_prompt or new_run_at_iso_datetime or new_cron_expression or new_steps or clear_steps):
        return {
            "status": "error",
            "message": "Nothing to update — provide at least one of new_task_prompt, "
                       "new_run_at_iso_datetime, new_cron_expression, new_steps, or clear_steps.",
        }
    if new_steps and clear_steps:
        return {
            "status": "error",
            "message": "new_steps and clear_steps are mutually exclusive — pick one.",
        }

    is_recurring = job.id.startswith("cron_") or job.id.startswith("sys_cron_")

    if new_cron_expression and not is_recurring:
        return {
            "status": "error",
            "message": f"Task {job_id} is a one-off — cannot set a cron expression. Delete and recreate as recurring.",
        }
    if new_run_at_iso_datetime and is_recurring:
        return {
            "status": "error",
            "message": f"Task {job_id} is recurring — use new_cron_expression instead. Delete and recreate to convert to one-off.",
        }

    # Preserve all original kwargs (notify carries ownership + delivery target; system tasks carry admin_user_id/silent)
    existing_kwargs = dict(job.kwargs or {})
    if new_task_prompt:
        existing_kwargs["task_prompt"] = new_task_prompt
    updated_prompt = existing_kwargs.get("task_prompt", "")

    # Enforced-plan updates
    if clear_steps:
        existing_kwargs.pop("steps", None)
    elif new_steps is not None:
        validated_steps = _validate_steps(new_steps)
        if isinstance(validated_steps, dict):
            return validated_steps
        if validated_steps:
            existing_kwargs["steps"] = validated_steps
        else:
            existing_kwargs.pop("steps", None)

    new_run_date = None
    new_trigger = None

    if new_run_at_iso_datetime:
        if not timezone:
            return {"status": "error", "message": "timezone is required when changing the run time."}
        tz = _parse_tz(timezone)
        if tz is None:
            return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}
        try:
            new_run_date = _parse_iso_to_tz(new_run_at_iso_datetime, tz)
        except (ValueError, TypeError):
            return {"status": "error", "message": "Invalid datetime format. Must be ISO 8601."}
        if new_run_date <= datetime.now(tz):
            return {"status": "error", "message": "Scheduled time is in the past."}

    if new_cron_expression:
        if not timezone:
            return {"status": "error", "message": "timezone is required when changing the cron expression."}
        tz = _parse_tz(timezone)
        if tz is None:
            return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}
        try:
            new_trigger = CronTrigger.from_crontab(new_cron_expression, timezone=tz)
        except ValueError:
            return {"status": "error", "message": "Invalid cron expression."}

    try:
        scheduler.modify_job(job_id, kwargs=existing_kwargs)
        if new_run_date is not None:
            scheduler.reschedule_job(job_id, trigger="date", run_date=new_run_date)
        elif new_trigger is not None:
            scheduler.reschedule_job(job_id, trigger=new_trigger)
    except Exception as e:
        return {"status": "error", "message": f"Failed to update {job_id}: {e}"}

    parts = [f"prompt='{updated_prompt}'"]
    if new_run_date is not None:
        parts.append(f"run_at={new_run_date.strftime('%Y-%m-%d %H:%M')} ({timezone})")
    if new_trigger is not None:
        parts.append(f"cron='{new_cron_expression}' ({timezone})")
    if clear_steps:
        parts.append("steps=<cleared, enforcement removed>")
    elif new_steps is not None:
        parts.append(f"steps={len(existing_kwargs.get('steps') or [])} enforced")
    return {
        "status": "success",
        "message": f"Updated task {job_id}: " + ", ".join(parts),
    }


# ---------------------------------------------------------------------------
# System (admin-only) scheduling tools
# ---------------------------------------------------------------------------

def schedule_system_task(
    task_prompt: str, run_at_iso_datetime: str, timezone: str, tool_context: ToolContext,
    silent: bool = False, deliver_to: str = "",
    steps: list[str] | None = None,
) -> dict:
    """Schedules a one-off system maintenance task that runs with admin privileges.

    Use this for admin-only system chores: security audits, backup verification, cleanup, health checks.
    These tasks run in an isolated session with full agent access (including DeveloperAgent delegation).
    IMPORTANT: Always call get_current_time first to know the current time before scheduling.

    Args:
        task_prompt (str): The exact system task instruction (e.g. 'Run a security audit on current .env permissions and report findings').
        run_at_iso_datetime (str): When to run, in ISO 8601 format (e.g., '2026-03-28T03:00:00').
        timezone (str): IANA timezone for the scheduled time (e.g., 'Europe/Kyiv', 'UTC').
        silent (bool): If True, only notify the admin on failure/warnings. Successes are logged silently. Default: False.
        deliver_to (str): Optional session ID to deliver results to instead of the current chat (e.g. 'sl_C01234ABC' for a Slack channel). If empty, delivers to the current chat.
        steps (list[str]): Optional ordered checklist. If provided, plan is seeded before the
            agent's first turn and the plan_executor workflow drives it step-by-step. See
            scheduling-skill → Enforced step-by-step scheduling.

    Returns:
        dict: Status of the scheduling operation.
    """
    from app.scheduler_instance import scheduler
    from app.tasks import run_system_task

    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can schedule system tasks."}

    tz = _parse_tz(timezone)
    if tz is None:
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        run_date = _parse_iso_to_tz(run_at_iso_datetime, tz)
    except (ValueError, TypeError):
        return {"status": "error", "message": "Invalid datetime format. Must be ISO 8601."}

    if run_date <= datetime.now(tz):
        return {"status": "error", "message": "Scheduled time is in the past."}

    notify = _resolve_notify(tool_context, deliver_to)
    if not notify:
        return {
            "status": "error",
            "message": "Cannot schedule: no delivery target could be resolved. "
                       "Provide a valid `deliver_to` session ID, or schedule from a chat that has a registered transport.",
        }

    validated_steps = _validate_steps(steps)
    if isinstance(validated_steps, dict):
        return validated_steps

    notify = _stamp_ownership(notify, "", deliver_to, _get_session_id(tool_context))

    job_id = f"sys_oneoff_{uuid.uuid4().hex[:8]}"
    job_kwargs = {
        "task_prompt": task_prompt,
        "notify": notify,
        "admin_user_id": admin_user_id,
        "silent": silent,
    }
    if validated_steps:
        job_kwargs["steps"] = validated_steps
    scheduler.add_job(
        run_system_task,
        "date",
        run_date=run_date,
        kwargs=job_kwargs,
        id=job_id,
    )

    dest = deliver_to or "current chat"
    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"System task scheduled: '{task_prompt}' for {run_date.strftime('%Y-%m-%d %H:%M')} ({timezone}). Delivers to: {dest}. Mode: {mode}. Job ID: {job_id}",
    }


def run_system_task_now(
    task_prompt: str, tool_context: ToolContext, silent: bool = False, deliver_to: str = "",
    steps: list[str] | None = None,
) -> dict:
    """Immediately launches a system maintenance task in the background with admin privileges.

    Use this instead of schedule_system_task when the task should start right away
    (e.g., 'evolve yourself', 'fix this bug now', 'run a health check').
    The task runs asynchronously — the user gets a confirmation immediately and
    receives the result via their notification channel when it completes.

    Args:
        task_prompt (str): The exact system task instruction (e.g. 'Analyze and fix the failing test in tests/test_structure.py').
        silent (bool): If True, only notify the admin on failure/warnings. Successes are logged silently. Default: False.
        deliver_to (str): Optional session ID to deliver results to instead of the current chat (e.g. 'sl_C01234ABC' for a Slack channel). If empty, delivers to the current chat.
        steps (list[str]): Optional ordered checklist. If provided, plan is seeded before the
            agent's first turn and the plan_executor workflow drives it step-by-step.

    Returns:
        dict: Confirmation that the task has been launched.
    """
    import asyncio

    from app.tasks import run_system_task

    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can run system tasks."}

    notify = _resolve_notify(tool_context, deliver_to)
    if not notify:
        return {
            "status": "error",
            "message": "Cannot launch: no delivery target could be resolved. "
                       "Provide a valid `deliver_to` session ID, or run from a chat that has a registered transport.",
        }

    validated_steps = _validate_steps(steps)
    if isinstance(validated_steps, dict):
        return validated_steps

    notify = _stamp_ownership(notify, "", deliver_to, _get_session_id(tool_context))

    task_id = f"immediate_{uuid.uuid4().hex[:8]}"

    asyncio.create_task(
        run_system_task(
            task_prompt=task_prompt,
            notify=notify,
            admin_user_id=admin_user_id,
            silent=silent,
            task_id=task_id,
            steps=validated_steps,
        ),
        name=task_id,
    )

    dest = deliver_to or "current chat"
    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"System task launched immediately: '{task_prompt}'. Delivers to: {dest}. Mode: {mode}. Task ID: {task_id}",
    }


def schedule_recurring_system_task(
    task_prompt: str, cron_expression: str, timezone: str, tool_context: ToolContext,
    silent: bool = False, deliver_to: str = "",
    steps: list[str] | None = None,
) -> dict:
    """Schedules a recurring system maintenance task that runs with admin privileges on a cron schedule.

    Use this for periodic admin-only chores: nightly security scans, daily backup verification,
    weekly log rotation, periodic health checks.
    IMPORTANT: Always call get_current_time first to confirm the user's timezone.

    Args:
        task_prompt (str): The exact system task instruction (e.g. 'Verify database backup integrity and report any corruption').
        cron_expression (str): A standard 5-part cron expression (e.g., '0 3 * * *' for every day at 3 AM).
        timezone (str): IANA timezone for the cron schedule (e.g., 'Europe/Kyiv', 'UTC').
        silent (bool): If True, only notify the admin on failure/warnings. Successes are logged silently. Default: False.
        deliver_to (str): Optional session ID to deliver results to instead of the current chat (e.g. 'sl_C01234ABC' for a Slack channel). If empty, delivers to the current chat.
        steps (list[str]): Optional ordered checklist. If provided, plan is seeded before the
            agent's first turn and the plan_executor workflow drives it step-by-step. See
            scheduling-skill → Enforced step-by-step scheduling.

    Returns:
        dict: Status of the scheduling operation.
    """
    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler_instance import scheduler
    from app.tasks import run_system_task

    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can schedule system tasks."}

    tz = _parse_tz(timezone)
    if tz is None:
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    dow_err = _validate_cron_dow(cron_expression)
    if dow_err:
        return {"status": "error", "message": dow_err}

    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid cron expression."}

    notify = _resolve_notify(tool_context, deliver_to)
    if not notify:
        return {
            "status": "error",
            "message": "Cannot schedule: no delivery target could be resolved. "
                       "Provide a valid `deliver_to` session ID, or schedule from a chat that has a registered transport.",
        }

    validated_steps = _validate_steps(steps)
    if isinstance(validated_steps, dict):
        return validated_steps

    notify = _stamp_ownership(notify, "", deliver_to, _get_session_id(tool_context))

    job_id = f"sys_cron_{uuid.uuid4().hex[:8]}"
    job_kwargs = {
        "task_prompt": task_prompt,
        "notify": notify,
        "admin_user_id": admin_user_id,
        "silent": silent,
    }
    if validated_steps:
        job_kwargs["steps"] = validated_steps
    scheduler.add_job(
        run_system_task,
        trigger=trigger,
        kwargs=job_kwargs,
        id=job_id,
    )

    dest = deliver_to or "current chat"
    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"Recurring system task scheduled: '{task_prompt}' with cron '{cron_expression}' ({timezone}). Delivers to: {dest}. Mode: {mode}. Job ID: {job_id}",
    }
