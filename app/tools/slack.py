from typing import Any

import httpx
from google.adk.tools.tool_context import ToolContext

from deploy import vault


def _get_headers():
    token = vault.get("SLACK_BOT_TOKEN")
    headers = {
        "Content-Type": "application/json; charset=utf-8"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def slack_post_message(channel: str, text: str, thread_ts: str | None = None, tool_context: ToolContext = None) -> dict[str, Any]:
    """Sends a message to a Slack channel.

    Args:
        channel (str): The ID of the channel to post to.
        text (str): The message text.
        thread_ts (str, optional): The timestamp of the parent message to reply to.

    Returns:
        dict: Response from Slack API.
    """
    payload = {
        "channel": channel,
        "text": text
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post("https://slack.com/api/chat.postMessage", json=payload, headers=_get_headers())
            data = response.json()
            if data.get("ok"):
                return {"status": "success", "ts": data["ts"], "channel": data["channel"]}
            else:
                return {"status": "error", "message": data.get("error", "Unknown error")}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def slack_list_channels(types: str = "public_channel,private_channel", tool_context: ToolContext = None) -> dict[str, Any]:
    """Lists all channels the bot has access to.

    Args:
        types (str): Comma-separated list of channel types.

    Returns:
        dict: List of channel objects.
    """
    channels = []
    cursor = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                params = {"types": types, "limit": 200}
                if cursor:
                    params["cursor"] = cursor

                response = await client.get("https://slack.com/api/conversations.list", params=params, headers=_get_headers())
                data = response.json()

                if not data.get("ok"):
                    return {"status": "error", "message": data.get("error", "Unknown error")}

                channels.extend(data.get("channels", []))
                cursor = data.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            return {"status": "success", "channels": channels}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def slack_read_history(channel: str, limit: int = 10, tool_context: ToolContext = None) -> dict[str, Any]:
    """Reads the most recent messages from a channel.

    Args:
        channel (str): The ID of the channel.
        limit (int): Max number of messages to return.

    Returns:
        dict: List of messages.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"channel": channel, "limit": limit}
            response = await client.get("https://slack.com/api/conversations.history", params=params, headers=_get_headers())
            data = response.json()

            if data.get("ok"):
                return {"status": "success", "messages": data["messages"]}
            else:
                return {"status": "error", "message": data.get("error", "Unknown error")}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def slack_read_replies(channel: str, thread_ts: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Retrieves all messages in a specific thread.

    Args:
        channel (str): The ID of the channel.
        thread_ts (str): The timestamp of the parent message.

    Returns:
        dict: List of messages in the thread.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"channel": channel, "ts": thread_ts}
            response = await client.get("https://slack.com/api/conversations.replies", params=params, headers=_get_headers())
            data = response.json()

            if data.get("ok"):
                return {"status": "success", "messages": data["messages"]}
            else:
                return {"status": "error", "message": data.get("error", "Unknown error")}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def slack_get_user_info(user: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Retrieves profile information for a Slack user ID.

    Args:
        user (str): The Slack user ID (e.g., U12345).

    Returns:
        dict: User profile information.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"user": user}
            response = await client.get("https://slack.com/api/users.info", params=params, headers=_get_headers())
            data = response.json()

            if data.get("ok"):
                return {"status": "success", "user": data["user"]}
            else:
                return {"status": "error", "message": data.get("error", "Unknown error")}
    except Exception as e:
        return {"status": "error", "message": str(e)}
