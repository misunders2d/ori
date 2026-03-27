import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def run_scheduled_task(task_prompt: str, notify: dict, is_actionable: bool = False):
    """
    Executed by APScheduler when a scheduled task fires.
    Runs the agent with the task prompt and delivers the response
    to the user via their original messaging channel.
    """
    from interfaces.telegram_poller import extract_agent_response
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
    """Send a message to the user via their original channel."""
    if not notify:
        logger.warning("Scheduled task fired but no notification channel configured")
        return

    channel_type = notify.get("type")

    async with httpx.AsyncClient(timeout=15) as client:
        if channel_type == "telegram":
            chat_id = notify.get("chat_id")
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if token and chat_id:
                from interfaces.telegram_poller import send_message as tg_send
                await tg_send(client, token, chat_id, message)
            else:
                logger.warning("Cannot deliver Telegram reminder: missing token or chat_id")

        elif channel_type == "slack":
            channel = notify.get("channel")
            token = os.environ.get("SLACK_BOT_TOKEN")
            if token and channel:
                try:
                    await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"channel": channel, "text": message},
                    )
                except Exception:
                    logger.exception("Failed to deliver Slack reminder")
            else:
                logger.warning("Cannot deliver Slack reminder: missing token or channel")
