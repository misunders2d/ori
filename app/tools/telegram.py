"""Telegram-specific agent tools.

The bot can DM only users who have previously messaged it (Telegram platform
rule — bots cannot initiate conversations). The roster (data/roster.json) is
auto-populated by the telegram poller on every authorized inbound message.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.runtime.roster import lookup_by_name
from app.runtime.transport import get_adapter

logger = logging.getLogger(__name__)


async def telegram_send_dm(
    person: str,
    text: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Send a direct message to a Telegram user by name or username.

    Resolves `person` against the local roster (auto-populated as users message
    the bot). If multiple roster entries match, returns the candidate list and
    asks the agent to disambiguate. The bot can only DM users who have messaged
    it at least once.

    Args:
        person: Display name, first/last name, or @username of the recipient.
        text: Message body.

    Returns:
        dict with `status`: "success" | "ambiguous" | "not_found" | "error".
    """
    matches = lookup_by_name(person, platform="telegram")
    if not matches:
        return {
            "status": "not_found",
            "message": (
                f"No telegram user matching '{person}' in the roster. "
                "Telegram bots can only DM users who have first messaged the bot. "
                "Ask the user to send any message to the bot, then retry."
            ),
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "message": f"Multiple telegram users match '{person}'. Disambiguate by user_id.",
            "candidates": [
                {
                    "user_id": m["user_id"],
                    "display_name": m.get("display_name"),
                    "username": m.get("username"),
                }
                for m in matches
            ],
        }
    entry = matches[0]
    adapter = get_adapter("telegram")
    if adapter is None:
        return {"status": "error", "message": "Telegram adapter is not registered (poller not running)."}
    try:
        await adapter.send_message(entry["chat_id"], text)
        return {
            "status": "success",
            "user_id": entry["user_id"],
            "display_name": entry.get("display_name"),
            "chat_id": entry["chat_id"],
        }
    except Exception as e:
        logger.exception("telegram_send_dm failed")
        return {"status": "error", "message": str(e)}
