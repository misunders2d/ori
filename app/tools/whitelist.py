import logging
from typing import Annotated

from app.core.whitelist import (
    blacklist_chat as _bl,
    get_blacklist,
    get_whitelist,
    unwhitelist_chat as _uw,
    whitelist_chat as _wl,
)

logger = logging.getLogger(__name__)

async def whitelist_chat(
    chat_id: Annotated[str, "The canonical ID of the chat, group, or channel to authorize (e.g., 'tg_123456' or 'tg_-100123456')"]
) -> str:
    """Authorize a user, group, or channel to interact with the bot."""
    _wl(chat_id)
    return f"Successfully whitelisted `{chat_id}`."

async def blacklist_chat(
    chat_id: Annotated[str, "The canonical ID of the chat, group, or channel to block (e.g., 'tg_123456')"]
) -> str:
    """Explicitly block a chat/user ID and silence all future access notifications from them."""
    _bl(chat_id)
    return f"Successfully blacklisted `{chat_id}`. I will no longer notify you about attempts from this ID."

async def unwhitelist_chat(
    chat_id: Annotated[str, "The canonical ID to remove from the whitelist."]
) -> str:
    """Revoke access for a previously whitelisted chat/user ID."""
    _uw(chat_id)
    return f"Successfully removed `{chat_id}` from the whitelist."

async def list_access_control() -> dict:
    """List all whitelisted and blacklisted IDs."""
    return {
        "whitelisted": get_whitelist(),
        "blacklisted": get_blacklist()
    }
