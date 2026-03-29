import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
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
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
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



def _get_session_notify_info(tool_context: ToolContext) -> dict:
    """Extract notification info (channel type + id) from the current session via the adapter registry."""
    from app.core.transport import parse_notify_from_session_id

    session = getattr(tool_context, "session", None)
    if not session:
        return {}
    sid = str(getattr(session, "id", ""))
    return parse_notify_from_session_id(sid)




def schedule_one_off_task(
    task_prompt: str, run_at_iso_datetime: str, timezone: str, tool_context: ToolContext, is_actionable: bool = False
) -> dict:
    """Schedules the agent to execute a specific task once at a specific date and time.

    Use this tool when the user asks to "remind me", "check this tomorrow", or "do X at Y time".
    IMPORTANT: Always call get_current_time first to know the current time before scheduling.

    Args:
        task_prompt (str): The exact instruction the agent should execute when the time comes (e.g. 'Remind the user to check Keepa for ASIN B08X').
        run_at_iso_datetime (str): The date and time to run the task, in ISO 8601 format (e.g., '2026-03-25T10:00:00'). This is in the timezone specified.
        timezone (str): IANA timezone for the scheduled time (e.g., 'Europe/Kyiv', 'UTC').
        is_actionable (bool): Set True if the task requires the agent to run background scripts or check APIs. Set False if it's purely a textual notification reminder.

    Returns:
        dict: Status of the scheduling operation.
    """
    from zoneinfo import ZoneInfo

    from app.scheduler_instance import scheduler
    from app.tasks import run_scheduled_task

    job_id = f"oneoff_{uuid.uuid4().hex[:8]}"

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        naive_dt = datetime.fromisoformat(run_at_iso_datetime.replace("Z", ""))
        run_date = naive_dt.replace(tzinfo=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid datetime format. Must be ISO 8601."}

    # Check it's in the future
    if run_date <= datetime.now(tz):
        return {"status": "error", "message": "Scheduled time is in the past."}

    notify = _get_session_notify_info(tool_context)

    scheduler.add_job(
        run_scheduled_task,
        "date",
        run_date=run_date,
        kwargs={"task_prompt": task_prompt, "notify": notify, "is_actionable": is_actionable},
        id=job_id,
    )

    return {
        "status": "success",
        "message": f"Scheduled: '{task_prompt}' for {run_date.strftime('%Y-%m-%d %H:%M')} ({timezone}). Job ID: {job_id}",
    }



def schedule_recurring_task(
    task_prompt: str, cron_expression: str, timezone: str, tool_context: ToolContext, is_actionable: bool = False
) -> dict:
    """Schedules the agent to execute a task automatically on a recurring schedule.

    Use this tool when the user asks to "regularly monitor", "do X every day", or "check X every Monday".
    IMPORTANT: Always call get_current_time first to confirm the user's timezone.

    Args:
        task_prompt (str): The exact instruction the agent should execute (e.g. 'Perform a management check for ASIN B08X').
        cron_expression (str): A standard 5-part cron expression defining the schedule (e.g., '0 10 * * *' for every day at 10 AM).
        timezone (str): IANA timezone for the cron schedule (e.g., 'Europe/Kyiv', 'UTC').
        is_actionable (bool): Set True if the task requires the agent to run background scripts or check APIs. Set False if it's purely a textual notification reminder.

    Returns:
        dict: Status of the scheduling operation.
    """
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler_instance import scheduler
    from app.tasks import run_scheduled_task

    job_id = f"cron_{uuid.uuid4().hex[:8]}"

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid cron expression."}

    notify = _get_session_notify_info(tool_context)

    scheduler.add_job(
        run_scheduled_task,
        trigger=trigger,
        kwargs={"task_prompt": task_prompt, "notify": notify, "is_actionable": is_actionable},
        id=job_id,
    )

    return {
        "status": "success",
        "message": f"Scheduled recurring: '{task_prompt}' with cron '{cron_expression}' ({timezone}). Job ID: {job_id}",
    }



def list_scheduled_tasks(tool_context: ToolContext) -> dict:
    """Lists all currently scheduled tasks/reminders.

    Use this when the user asks to see their reminders, scheduled tasks, or wants to know what's coming up.

    Returns:
        dict: List of scheduled tasks with their details.
    """
    from app.scheduler_instance import scheduler

    jobs = scheduler.get_jobs()
    if not jobs:
        return {"status": "success", "tasks": [], "message": "No scheduled tasks."}

    tasks = []
    for job in jobs:
        task_info = {
            "job_id": job.id,
            "task": job.args[0] if job.args else "Unknown",
            "next_run": str(job.next_run_time) if job.next_run_time else "N/A",
            "type": (
                "system (recurring)" if job.id.startswith("sys_cron_")
                else "system (one-off)" if job.id.startswith("sys_oneoff_")
                else "recurring" if job.id.startswith("cron_")
                else "one-off"
            ),
        }
        tasks.append(task_info)

    return {"status": "success", "tasks": tasks}



def delete_scheduled_task(job_id: str, tool_context: ToolContext) -> dict:
    """Deletes a scheduled task/reminder.

    Use this when the user wants to cancel a reminder or stop a recurring task.
    Call list_scheduled_tasks first to get the job_id.

    Args:
        job_id (str): The job ID to delete (e.g., 'oneoff_a1b2c3d4' or 'cron_e5f6g7h8').

    Returns:
        dict: Status of the deletion.
    """
    from app.scheduler_instance import scheduler

    try:
        scheduler.remove_job(job_id)
        return {"status": "success", "message": f"Deleted task {job_id}."}
    except Exception:
        return {"status": "error", "message": f"Task {job_id} not found."}



def edit_scheduled_task(
    job_id: str, new_task_prompt: str, new_run_at_iso_datetime: str,
    timezone: str, tool_context: ToolContext, is_actionable: bool = False
) -> dict:
    """Edits an existing one-off scheduled task — changes its prompt and/or time.

    Call list_scheduled_tasks first to get the job_id.

    Args:
        job_id (str): The job ID to edit.
        new_task_prompt (str): The updated task instruction.
        new_run_at_iso_datetime (str): The new date and time in ISO 8601 format.
        timezone (str): IANA timezone for the new time.
        is_actionable (bool): Set True if the task requires the agent to run background scripts or check APIs. Set False if it's purely a textual notification reminder.

    Returns:
        dict: Status of the edit.
    """
    from zoneinfo import ZoneInfo

    from app.scheduler_instance import scheduler
    from app.tasks import run_scheduled_task

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        naive_dt = datetime.fromisoformat(new_run_at_iso_datetime.replace("Z", ""))
        run_date = naive_dt.replace(tzinfo=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid datetime format."}

    if run_date <= datetime.now(tz):
        return {"status": "error", "message": "Scheduled time is in the past."}

    notify = _get_session_notify_info(tool_context)

    try:
        scheduler.remove_job(job_id)
    except Exception:
        return {"status": "error", "message": f"Task {job_id} not found."}

    scheduler.add_job(
        run_scheduled_task,
        "date",
        run_date=run_date,
        kwargs={"task_prompt": new_task_prompt, "notify": notify, "is_actionable": is_actionable},
        id=job_id,
    )

    return {
        "status": "success",
        "message": f"Updated task {job_id}: '{new_task_prompt}' at {run_date.strftime('%Y-%m-%d %H:%M')} ({timezone}).",
    }


def _require_admin(tool_context: ToolContext) -> str | None:
    """Check if the current user is an admin. Returns the admin user_id if authorized, None otherwise."""
    session = getattr(tool_context, "session", None)
    if not session:
        return None
    state = session.state if hasattr(session, "state") else {}
    if isinstance(state, dict):
        user_id = state.get("user_id", "")
    else:
        user_id = getattr(state, "get", lambda k, d: d)("user_id", "")

    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    if admin_users and user_id in admin_users:
        return user_id
    return None


def schedule_system_task(
    task_prompt: str, run_at_iso_datetime: str, timezone: str, tool_context: ToolContext, silent: bool = False
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

    Returns:
        dict: Status of the scheduling operation.
    """
    from zoneinfo import ZoneInfo

    from app.scheduler_instance import scheduler
    from app.tasks import run_system_task

    # Admin-only enforcement
    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can schedule system tasks."}

    job_id = f"sys_oneoff_{uuid.uuid4().hex[:8]}"

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        naive_dt = datetime.fromisoformat(run_at_iso_datetime.replace("Z", ""))
        run_date = naive_dt.replace(tzinfo=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid datetime format. Must be ISO 8601."}

    if run_date <= datetime.now(tz):
        return {"status": "error", "message": "Scheduled time is in the past."}

    notify = _get_session_notify_info(tool_context)

    scheduler.add_job(
        run_system_task,
        "date",
        run_date=run_date,
        kwargs={
            "task_prompt": task_prompt,
            "notify": notify,
            "admin_user_id": admin_user_id,
            "silent": silent,
        },
        id=job_id,
    )

    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"System task scheduled: '{task_prompt}' for {run_date.strftime('%Y-%m-%d %H:%M')} ({timezone}). Mode: {mode}. Job ID: {job_id}",
    }


def run_system_task_now(
    task_prompt: str, tool_context: ToolContext, silent: bool = False
) -> dict:
    """Immediately launches a system maintenance task in the background with admin privileges.

    Use this instead of schedule_system_task when the task should start right away
    (e.g., 'evolve yourself', 'fix this bug now', 'run a health check').
    The task runs asynchronously — the user gets a confirmation immediately and
    receives the result via their notification channel when it completes.

    Args:
        task_prompt (str): The exact system task instruction (e.g. 'Analyze and fix the failing test in tests/test_structure.py').
        silent (bool): If True, only notify the admin on failure/warnings. Successes are logged silently. Default: False.

    Returns:
        dict: Confirmation that the task has been launched.
    """
    import asyncio

    from app.tasks import run_system_task

    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can run system tasks."}

    notify = _get_session_notify_info(tool_context)
    task_id = f"immediate_{uuid.uuid4().hex[:8]}"

    asyncio.create_task(
        run_system_task(
            task_prompt=task_prompt,
            notify=notify,
            admin_user_id=admin_user_id,
            silent=silent,
        ),
        name=task_id,
    )

    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"System task launched immediately: '{task_prompt}'. Mode: {mode}. Task ID: {task_id}",
    }


def schedule_recurring_system_task(
    task_prompt: str, cron_expression: str, timezone: str, tool_context: ToolContext, silent: bool = False
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

    Returns:
        dict: Status of the scheduling operation.
    """
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler_instance import scheduler
    from app.tasks import run_system_task

    # Admin-only enforcement
    admin_user_id = _require_admin(tool_context)
    if not admin_user_id:
        return {"status": "error", "message": "Only admin users can schedule system tasks."}

    job_id = f"sys_cron_{uuid.uuid4().hex[:8]}"

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, Exception):
        return {"status": "error", "message": f"Unknown timezone: '{timezone}'."}

    try:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=tz)
    except ValueError:
        return {"status": "error", "message": "Invalid cron expression."}

    notify = _get_session_notify_info(tool_context)

    scheduler.add_job(
        run_system_task,
        trigger=trigger,
        kwargs={
            "task_prompt": task_prompt,
            "notify": notify,
            "admin_user_id": admin_user_id,
            "silent": silent,
        },
        id=job_id,
    )

    mode = "silent (notify on failure only)" if silent else "verbose (always notify)"
    return {
        "status": "success",
        "message": f"Recurring system task scheduled: '{task_prompt}' with cron '{cron_expression}' ({timezone}). Mode: {mode}. Job ID: {job_id}",
    }

