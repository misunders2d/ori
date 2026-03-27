import logging
import os

logger = logging.getLogger(__name__)


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
