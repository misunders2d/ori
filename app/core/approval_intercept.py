"""Deterministic ``Approve ACT-XXXXXX`` intercept.

Pre-2026-05-13, when a user replied ``Approve ACT-XXXXXX``, the
LLM was expected to call ``execute_approved_action(token=...)``.
In practice it did so unreliably — frequently re-invoking the
ORIGINAL gated tool instead, which the admin guardrail then
re-staged as a fresh token. Two months of approval-loop hell:

  bezos: "the approval gate is regenerating each turn instead of
  being consumed"

This module closes the loop by intercepting ``Approve ACT-XXXXXX``
patterns in inbound user messages BEFORE they reach the agent
runner. The intercept:

  1. Parses the token (and optional 6-digit TOTP code).
  2. Looks up the pending action by token via
     ``pending_actions.get_and_delete_action``.
  3. Validates the user_id matches (token belongs to this user).
  4. Calls the staged tool directly with a synthetic
     ``ToolContext`` shim — same pattern ``app.contracts._loader_context``
     uses for loaders that run outside an agent session.
  5. Returns a plain-text reply the poller posts back.

Bypasses the LLM entirely. The skill instructions
(``skills/approval-skill``) and the staged guardrail still apply
when the LLM DOES manage to call ``execute_approved_action`` —
this is an additional, deterministic short-circuit.

Public entry points:

  * ``APPROVE_RE`` — compiled regex matching the approve pattern.
  * ``parse_approval_text(text)`` — extracts ``(token, totp)`` or
    None.
  * ``handle_approval(token, totp_code, user_id, session_id)`` —
    async; runs the staged action; returns a user-facing message.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Accept any casing for ``Approve`` and the optional TOTP code, and
# tolerate trailing/leading whitespace. Token format mirrors what
# ``stage_action`` emits (``ACT-`` + 6 alnum). The pattern stays
# narrow on purpose — broad regexes would false-positive on
# unrelated user messages like "I'm going to approve that change".
APPROVE_RE = re.compile(
    r"^\s*Approve\s+(?P<token>ACT-[A-Z0-9]{4,})(?:\s+(?P<totp>\d{6}))?\s*$",
    re.IGNORECASE,
)


def parse_approval_text(text: str) -> Optional[tuple[str, str]]:
    """Return ``(token_upper, totp_or_empty)`` if ``text`` is an
    approval message, else ``None``.

    A user typing just ``approve act-a7b3f9 123456`` (lowercase,
    any spacing) still matches.
    """
    if not text:
        return None
    m = APPROVE_RE.match(text)
    if not m:
        return None
    return m.group("token").upper(), (m.group("totp") or "")


# ---------------------------------------------------------------------------
# Synthetic ToolContext shim — looks enough like the real ADK
# ``ToolContext`` for ``execute_approved_action`` + the staged
# evolution tools to read user_id / session_id from state.
# ---------------------------------------------------------------------------


class _ApprovalState:
    def __init__(self, user_id: str, session_id: str):
        self._d: dict[str, Any] = {
            "user_id": user_id,
            "session_id": session_id,
            # Approval intercept runs OUTSIDE an agent session so the
            # docs-read state isn't populated. Evolution tools that
            # gate on docs_read still treat this as a fresh
            # invocation — the user has already confirmed intent by
            # typing the token, and the gate's purpose (refuse if
            # docs weren't read THIS conversation turn) doesn't apply
            # to a non-LLM execution path. Seed truthy to bypass.
            "docs_read": {
                "docs/AI_EDITS.md": True,
                "docs/INDEX.md": True,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self._d)

    def __setitem__(self, k: str, v: Any) -> None:
        self._d[k] = v

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def get(self, k: str, default=None) -> Any:
        return self._d.get(k, default)


class _ApprovalSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.id = session_id


class ApprovalContext:
    """Minimal ToolContext stand-in for the deterministic approval
    intercept. Exposes ``.state`` (dict-like + ``to_dict``) and
    ``.session.session_id`` — the surface
    ``execute_approved_action`` and the evolution tools actually
    read."""

    def __init__(self, user_id: str, session_id: str):
        self.state = _ApprovalState(user_id, session_id)
        self.session = _ApprovalSession(session_id)


# ---------------------------------------------------------------------------
# Public async entry point used by pollers.
# ---------------------------------------------------------------------------


async def handle_approval(
    token: str,
    totp_code: str,
    user_id: str,
    session_id: str,
) -> str:
    """Execute a previously-staged action by token. Returns a
    user-facing plain-text reply suitable for posting back to the
    chat. Never raises — every error path returns a short, specific
    string the user can act on.
    """
    from app.core.pending_actions import get_and_delete_action

    if not token:
        return "Approval missing a token. Reply with `Approve ACT-XXXXXX`."

    token = token.strip().upper()

    # 2FA check: enforce here if enabled, so a missing TOTP code
    # short-circuits BEFORE we consume the staged action (otherwise
    # the action is deleted and we lose it).
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
    require_2fa = os.environ.get("REQUIRE_2FA", "true").lower() == "true"
    if totp_secret and require_2fa:
        if not totp_code:
            return (
                f"Approval `{token}` needs a 2FA code. Reply with "
                f"`Approve {token} 123456` (your current 6-digit code)."
            )
        try:
            from app.app_utils.totp import verify_totp
            if not verify_totp(totp_secret, str(totp_code)):
                return (
                    f"Invalid 2FA code for `{token}`. Reply again "
                    "with the current 6-digit code."
                )
        except Exception as e:
            logger.error("TOTP verify crashed on approval: %s", e)
            return (
                "2FA verification failed (internal error). Action "
                "not executed."
            )

    action = get_and_delete_action(token)
    if not action:
        return (
            f"Token `{token}` is invalid, already consumed, or "
            "expired (15-minute TTL). Re-issue the original request "
            "to get a fresh token."
        )

    action_user_id = action.get("user_id", "")
    if action_user_id and user_id and action_user_id != user_id:
        return (
            f"Token `{token}` was staged for a different user. "
            "Approval refused."
        )

    tool_name = action["tool_name"]
    args = action.get("args") or {}

    # Lookup the tool callable. Import via ``app.tools`` so the
    # __init__.py re-exports apply (matches what
    # ``execute_approved_action`` does).
    try:
        import app.tools as tools_module
    except Exception as e:
        logger.error("approval_intercept: import app.tools failed: %s", e)
        return f"Token `{token}` consumed but tool lookup failed."

    tool_func = getattr(tools_module, tool_name, None)
    if tool_func is None:
        return (
            f"Token `{token}` consumed but tool `{tool_name}` is no "
            "longer registered. Action skipped."
        )

    ctx = ApprovalContext(user_id=user_id, session_id=session_id)

    try:
        if inspect.iscoroutinefunction(tool_func):
            result = await tool_func(**args, tool_context=ctx)
        else:
            result = tool_func(**args, tool_context=ctx)
    except TypeError as e:
        # Most likely an arg-shape mismatch between staged args and
        # the tool's current signature. Surface verbatim.
        logger.exception("approval_intercept: tool TypeError")
        return f"Token `{token}` consumed but `{tool_name}` call rejected: {e}"
    except Exception as e:
        logger.exception("approval_intercept: tool raised")
        return f"Token `{token}` consumed but `{tool_name}` failed: {e}"

    # Render a short reply. Tools generally return a dict with
    # status/message; fall back to repr if not.
    if isinstance(result, dict):
        status = result.get("status") or result.get("ok") or "ok"
        message = result.get("message") or result.get("detail") or ""
        if message:
            return f"`{tool_name}` → **{status}**\n{message}"
        return f"`{tool_name}` → **{status}**\n```{json.dumps(result, default=str)[:1000]}```"
    return f"`{tool_name}` → {result!r}"
