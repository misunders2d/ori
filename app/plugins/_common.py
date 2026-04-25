"""Shared helpers for the plugin layer.

Tiny utilities that several plugins need. Kept in one place to avoid
copy-paste drift (e.g., admin-id parsing, A2A user detection).
"""

from __future__ import annotations

import os


def admin_user_ids() -> list[str]:
    """Parse ADMIN_USER_IDS env var into a clean list."""
    raw = os.environ.get("ADMIN_USER_IDS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def is_a2a_user(user_id: str) -> bool:
    """A2A callers carry a validated API key (checked at the A2A executor
    layer). Their user_id is prefixed with 'A2A_USER_' — trusted by default."""
    return bool(user_id) and user_id.startswith("A2A_USER_")


def state_user_id(callback_context) -> str:
    """Pull the speaker's user_id from session state. Empty string when absent."""
    state = callback_context.state.to_dict() if callback_context.state else {}
    return state.get("user_id") or ""


def state_session_id(callback_context) -> str | None:
    """Best-effort session id resolution from a CallbackContext."""
    sess = getattr(callback_context, "session", None)
    if sess is None:
        return None
    return getattr(sess, "session_id", None) or getattr(sess, "id", None)


def env_truthy(name: str, default: bool = False) -> bool:
    """`true`/`1`/`yes`/`on` parsed case-insensitively. Anything else = False."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")
