"""In-process pending-OAuth state map.

Single-use bindings from a `state_token` (random, issued at flow start by
`tools/integrations.configure_integration`) to the originating user_id /
session_id. Consumed by the A2A server's `/oauth/<provider>/callback`
handler so the resulting credential is saved under the correct user.

Tiny module — no agent/runner imports — so both `app.a2a_server` and
`app.tools.integrations` can use it without triggering each other's
heavy module init.
"""

from __future__ import annotations

_PENDING: dict[str, dict[str, str]] = {}


def register_pending_oauth(state_token: str, user_id: str, session_id: str) -> None:
    """Store a binding from `state_token` (random) to (user_id, session_id).

    Called by `configure_integration` when issuing an authorize URL.
    """
    _PENDING[state_token] = {"user_id": user_id, "session_id": session_id}


def consume_pending_oauth(state_token: str) -> dict[str, str] | None:
    """Pop and return the binding for `state_token` (single-use).

    Called by the OAuth callback handler. Returns None if the token isn't
    recognized (already consumed, expired, or never registered).
    """
    return _PENDING.pop(state_token, None)


def list_pending_tokens() -> list[str]:
    """Diagnostic — list outstanding tokens. Not used in production paths."""
    return list(_PENDING.keys())


def clear_all() -> None:
    """Test helper — wipe all pending bindings."""
    _PENDING.clear()
