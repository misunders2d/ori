"""
Secure key capture system.

When the agent needs a user to provide an API key, it registers a pending
capture for that session. The next message from that session is intercepted
at the transport layer (Telegram poller, Slack handler, etc.) BEFORE it
reaches the agent/LLM, saved to .env, and deleted from chat if possible.

The key never touches the AI, session history, or logs.
"""

import logging
import os

from dotenv import set_key

from app.app_utils.config import ALLOWED_CONFIG_KEYS, ENV_FILE_PATH

logger = logging.getLogger(__name__)

# Pending captures: session_id -> key_name
_pending: dict[str, str] = {}


def expect_key(session_id: str, key_name: str):
    """Register that the next message from this session should be captured as a config key."""
    _pending[session_id] = key_name


def check_pending(session_id: str) -> str | None:
    """Check if there's a pending key capture for this session. Returns key_name or None."""
    return _pending.get(session_id)


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

    set_key(ENV_FILE_PATH, key_name, value)
    os.environ[key_name] = value

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
