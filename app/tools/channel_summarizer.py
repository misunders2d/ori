import logging
from typing import Annotated
from datetime import datetime
from google import genai
from app.core.channel_logger import get_logs

logger = logging.getLogger(__name__)

async def summarize_channel(
    channel_id: Annotated[str, "The canonical ID of the channel to summarize (e.g., 'tg_-100123456')"],
    hours: Annotated[int, "Number of recent hours to summarize (default 24)"] = 24,
    limit: Annotated[int, "Maximum number of messages to retrieve from the local log"] = 200
) -> str:
    """Read the local logs for a whitelisted channel and generate a news summary."""
    logs = get_logs(channel_id, limit=limit, hours=hours)
    
    if not logs:
        return f"No messages found for channel `{channel_id}` in the last {hours} hours."

    # Format the log for the LLM
    # Note: logs are returned newest first, so we reverse for chronological order
    formatted_logs = []
    for log in reversed(logs):
        formatted_logs.append(f"[{log['timestamp']}] {log['display_name'] or 'Unknown'}: {log['text']}")
    
    context_text = "\n".join(formatted_logs)
    
    # Use a cheap model for summarization
    client = genai.Client()
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash-lite-preview-02-05",
            contents=(
                f"You are a threat analyst and news aggregator. Summarize the following messages from channel '{channel_id}' "
                f"covering the last {hours} hours. Focus on key news, potential threats, and significant events. "
                "Keep it professional and concise.\n\n" + context_text
            ),
        )
        summary = response.text or "I read the logs but couldn't generate a summary."
        return f"### Summary for {channel_id} (Last {hours}h)\n\n{summary}"
    except Exception as e:
        logger.error("Failed to generate channel summary: %s", e)
        return f"Error generating summary: {str(e)}"
