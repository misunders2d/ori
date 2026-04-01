import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# In-memory registry for tracking real-time status of background tasks
ACTIVE_TASKS = {}


async def run_scheduled_task(task_prompt: str, notify: dict, is_actionable: bool = False, task_id: str = None):
    """
    Executed by APScheduler when a scheduled task fires.
    Runs the agent with the task prompt and delivers the response
    to the user via their original messaging channel.
    """
    from app.core.agent_executor import extract_agent_response
    from run_bot import get_runner
    import uuid

    if not task_id:
        task_id = f"sched_{uuid.uuid4().hex[:8]}"

    ACTIVE_TASKS[task_id] = {
        "prompt": task_prompt,
        "type": "scheduled",
        "status": "Running",
        "start_time": datetime.now().isoformat(),
        "end_time": None,
        "error": None
    }

    runner = get_runner()

    # Build the response — either from the agent or just the raw prompt for simple reminders
    if not is_actionable:
        response = f"Reminder: {task_prompt}"
        ACTIVE_TASKS[task_id]["status"] = "Completed"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
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
            ACTIVE_TASKS[task_id]["status"] = "Completed"
            ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        except Exception as e:
            logger.exception("Scheduled task agent execution failed")
            response = f"Reminder: {task_prompt}"
            ACTIVE_TASKS[task_id]["status"] = "Failed"
            ACTIVE_TASKS[task_id]["error"] = str(e)
            ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
    else:
        response = f"Reminder: {task_prompt}"
        ACTIVE_TASKS[task_id]["status"] = "Completed"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()

    # Deliver to the user's channel
    await _deliver_message(notify, response)


async def run_system_task(task_prompt: str, notify: dict, admin_user_id: str, silent: bool = False, task_id: str = None):
    """
    Executed by APScheduler for admin-only system maintenance tasks.
    Runs the agent with full privileges in an isolated session, then cleans up.
    """
    import uuid

    from app.core.agent_executor import extract_agent_response, update_session_state
    from run_bot import get_runner

    if not task_id:
        task_id = f"sys_{uuid.uuid4().hex[:8]}"

    logger.info("System Task: Starting %s (%s)", task_id, task_prompt)

    ACTIVE_TASKS[task_id] = {
        "prompt": task_prompt,
        "type": "system",
        "status": "Running",
        "start_time": datetime.now().isoformat(),
        "end_time": None,
        "error": None
    }

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

        # Inject admin identity so guardrails recognize this as an admin execution
        await update_session_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            state_delta={"user_id": admin_user_id},
        )

        logger.info("System Task: Executing agent for %s", task_id)
        response = await extract_agent_response(runner, user_id, session_id, query)

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
            await _deliver_message(notify, msg)

        ACTIVE_TASKS[task_id]["status"] = "Completed" if not is_failure else "Completed (With Warnings)"
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
        logger.info("System Task: Finished %s", task_id)

    except Exception as e:
        logger.exception("System task execution failed: %s", task_prompt)
        ACTIVE_TASKS[task_id]["status"] = "Failed"
        ACTIVE_TASKS[task_id]["error"] = str(e)
        ACTIVE_TASKS[task_id]["end_time"] = datetime.now().isoformat()
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
        logger.warning("Notification delivery skipped: no notification info (notify={})", notify)
        return

    channel_type = notify.get("type")
    adapter = get_adapter(channel_type)
    if adapter:
        target = notify.get("chat_id") or notify.get("channel")
        logger.info("Delivering message to %s channel, target: %s", channel_type, target)
        try:
            await adapter.send_message(target, message)
        except Exception as e:
            logger.error("Failed to deliver message via adapter: %s", e)
    else:
        logger.warning("No adapter registered for channel type: %s", channel_type)
