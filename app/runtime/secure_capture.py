"""
Secure key capture system.

When the agent needs a user to provide an API key, it registers a pending
capture for that session. The next message from that session is intercepted
at the transport layer (Telegram poller, Slack handler, etc.) BEFORE it
reaches the agent/LLM, saved to .env, and deleted from chat if possible.

The key never touches the AI, session history, or logs.
"""

import json
import logging
import os

from app.util.config import ALLOWED_CONFIG_KEYS
from deploy.vault import set as vault_set

logger = logging.getLogger(__name__)

# Pending captures: session_id -> key_name
_pending: dict[str, str] = {}
# Pending friend keys: session_id -> friend_name
_pending_friend: dict[str, str] = {}

KEYS_FILE = os.path.abspath("./data/a2a_keys.json")

def expect_key(session_id: str, key_name: str):
    """Register that the next message from this session should be captured as a config key."""
    _pending[session_id] = key_name


def check_pending(session_id: str) -> str | None:
    """Check if there's a pending key capture for this session. Returns key_name or None."""
    return _pending.get(session_id)


def expect_friend_key(session_id: str, friend_name: str):
    """Register that the next message from this session should be captured as an A2A friend API key."""
    _pending_friend[session_id] = friend_name


def check_pending_friend(session_id: str) -> str | None:
    """Check if there's a pending friend key capture for this session. Returns friend_name or None."""
    return _pending_friend.get(session_id)


def capture_key(session_id: str, value: str) -> dict:
    """Consume the pending capture and save the key. Returns a status dict."""
    key_name = _pending.pop(session_id, None)
    if not key_name:
        return {"status": "error", "message": "No pending key capture for this session."}

    value = value.strip()
    if not value:
        # Re-register so next message is still captured
        _pending[session_id] = key_name
        return {"status": "retry", "key_name": key_name, "message": "Empty value. Please send the key again."}

    if key_name not in ALLOWED_CONFIG_KEYS:
        return {"status": "error", "message": f"Internal error: unknown key {key_name}."}

    vault_set(key_name, value)

    # Restart Telegram poller if messaging config changed
    if key_name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET"):
        try:
            from main import _start_telegram_poller
            _start_telegram_poller()
        except Exception:
            pass

    return {
        "status": "success",
        "key_name": key_name,
        "message": f"Configured {key_name} successfully. Your message has been deleted for security.",
    }


def capture_friend_key(session_id: str, value: str) -> dict:
    """Consume the pending friend key capture and save it to a2a_keys.json. Returns a status dict."""
    friend_name = _pending_friend.pop(session_id, None)
    if not friend_name:
        return {"status": "error", "message": "No pending friend key capture for this session."}

    value = value.strip()
    if not value:
        _pending_friend[session_id] = friend_name
        return {"status": "retry", "friend_name": friend_name, "message": "Empty value. Please send the API key again."}

    try:
        keys = {}
        if os.path.exists(KEYS_FILE):
            with open(KEYS_FILE) as f:
                keys = json.load(f)

        keys[friend_name] = value

        os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
        with open(KEYS_FILE, "w") as f:
            json.dump(keys, f, indent=4)

        return {
            "status": "success",
            "friend_name": friend_name,
            "message": f"API key for '{friend_name}' configured successfully. Your message has been deleted for security.",
        }
    except Exception as e:
        logger.error(f"Failed to capture friend key for {friend_name}: {e}")
        return {"status": "error", "message": f"Failed to save API key for {friend_name}: {e}"}
