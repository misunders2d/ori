"""Tests for the deterministic ``Approve ACT-XXXXXX`` intercept.

The intercept replaces the LLM round-trip that, pre-2026-05-13,
repeatedly failed: the LLM was supposed to call
``execute_approved_action(token=...)`` when the user typed
``Approve ACT-XXXXXX``, but it kept re-invoking the originally-
gated tool. The guardrail then staged a fresh token each turn and
the user's earlier approval became useless. Two months of loop.

These tests pin:
  * ``parse_approval_text`` recognises every reasonable shape
    (case-insensitive, optional 6-digit TOTP, leading/trailing
    whitespace) and refuses look-alikes (``I approve``, no token).
  * ``handle_approval`` consumes a staged token, runs the staged
    tool with a synthetic ``ApprovalContext``, and returns a clean
    plain-text reply on every error path.
  * ``stage_action`` is idempotent — same ``(tool, user, session,
    args)`` returns the same token until it expires.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated_pending_db(tmp_path, monkeypatch):
    """Redirect ``pending_actions.DB_PATH`` to a per-test SQLite file."""
    db_path = tmp_path / "pending_actions.db"
    monkeypatch.setattr(
        "app.core.pending_actions.DB_PATH", str(db_path)
    )
    monkeypatch.delenv("ADMIN_TOTP_SECRET", raising=False)
    monkeypatch.delenv("REQUIRE_2FA", raising=False)
    return db_path


# ---------------------------------------------------------------------------
# parse_approval_text
# ---------------------------------------------------------------------------


def test_parse_approval_basic():
    from app.core.approval_intercept import parse_approval_text

    assert parse_approval_text("Approve ACT-ABCDEF") == ("ACT-ABCDEF", "")


def test_parse_approval_case_insensitive():
    from app.core.approval_intercept import parse_approval_text

    assert parse_approval_text("approve act-abcdef") == ("ACT-ABCDEF", "")
    assert parse_approval_text("APPROVE ACT-ABCDEF") == ("ACT-ABCDEF", "")


def test_parse_approval_with_totp():
    from app.core.approval_intercept import parse_approval_text

    assert parse_approval_text("Approve ACT-ABCDEF 123456") == (
        "ACT-ABCDEF",
        "123456",
    )


def test_parse_approval_whitespace_tolerant():
    from app.core.approval_intercept import parse_approval_text

    assert parse_approval_text("   Approve   ACT-ABCDEF  ") == (
        "ACT-ABCDEF",
        "",
    )


def test_parse_approval_rejects_lookalikes():
    from app.core.approval_intercept import parse_approval_text

    assert parse_approval_text("I approve that change") is None
    assert parse_approval_text("Approve please") is None
    assert parse_approval_text("Approve ACT-") is None
    assert parse_approval_text("") is None
    assert parse_approval_text(None) is None  # type: ignore[arg-type]


def test_parse_approval_rejects_partial_totp():
    """5-digit code is not a valid TOTP — pattern requires exactly 6."""
    from app.core.approval_intercept import parse_approval_text

    # "ACT-ABCDEF" then a 5-digit number — pattern rejects because the
    # totp group requires exactly 6 digits AND nothing else trails.
    assert parse_approval_text("Approve ACT-ABCDEF 12345") is None


# ---------------------------------------------------------------------------
# stage_action idempotency
# ---------------------------------------------------------------------------


def test_stage_action_returns_existing_token_for_same_args(isolated_pending_db):
    """Same ``(tool, user, session, args)`` → same token until expired.
    Direct repro fix for the 2026-05-13 approval-gate loop."""
    from app.core.pending_actions import stage_action

    t1 = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_admin", "tg_session_x"
    )
    t2 = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_admin", "tg_session_x"
    )
    assert t1 == t2


def test_stage_action_new_token_for_different_args(isolated_pending_db):
    from app.core.pending_actions import stage_action

    t1 = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_admin", "tg_session_x"
    )
    t2 = stage_action(
        "evolution_commit_and_push", {"branch": "feature"}, "tg_admin", "tg_session_x"
    )
    assert t1 != t2


def test_stage_action_new_token_for_different_user(isolated_pending_db):
    from app.core.pending_actions import stage_action

    t1 = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_alice", "session_x"
    )
    t2 = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_bob", "session_x"
    )
    assert t1 != t2


def test_stage_action_args_normalised_for_idempotency(isolated_pending_db):
    """``json.dumps(sort_keys=True)`` normalises dict key order so
    ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` resolve to the same token."""
    from app.core.pending_actions import stage_action

    t1 = stage_action("X", {"a": 1, "b": 2}, "u", "s")
    t2 = stage_action("X", {"b": 2, "a": 1}, "u", "s")
    assert t1 == t2


def test_stage_action_new_token_after_expiry(isolated_pending_db, monkeypatch):
    """Expired tokens are NOT reused — a fresh one is issued."""
    from app.core.pending_actions import stage_action

    t1 = stage_action("X", {"a": 1}, "u", "s", ttl_minutes=15)

    # Manually expire the row.
    with sqlite3.connect(isolated_pending_db) as conn:
        conn.execute(
            "UPDATE pending_actions SET expires_at = ? WHERE token = ?",
            (time.time() - 60, t1),
        )

    t2 = stage_action("X", {"a": 1}, "u", "s")
    assert t1 != t2


# ---------------------------------------------------------------------------
# handle_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_approval_unknown_token_returns_friendly_error(
    isolated_pending_db,
):
    from app.core.approval_intercept import handle_approval

    reply = await handle_approval(
        token="ACT-NOPE", totp_code="", user_id="u", session_id="s"
    )
    assert "expired" in reply or "invalid" in reply.lower()


@pytest.mark.asyncio
async def test_handle_approval_executes_staged_tool(
    isolated_pending_db, monkeypatch
):
    """End-to-end happy path: stage an action, intercept the approval,
    confirm the staged tool ran with the staged args + a synthetic
    ToolContext that exposes the user's id."""
    from app.core import approval_intercept
    from app.core.pending_actions import stage_action

    called: dict[str, Any] = {}

    def fake_tool(branch: str, tool_context=None):
        called["branch"] = branch
        called["user_id"] = (
            tool_context.state.to_dict().get("user_id") if tool_context else None
        )
        called["session_id"] = (
            tool_context.state.to_dict().get("session_id") if tool_context else None
        )
        return {"status": "success", "message": "pushed"}

    # Stub the tools module attribute lookup.
    import app.tools as tools_module

    monkeypatch.setattr(tools_module, "evolution_commit_and_push", fake_tool, raising=False)

    token = stage_action(
        "evolution_commit_and_push",
        {"branch": "main"},
        "tg_admin",
        "tg_session_x",
    )

    reply = await approval_intercept.handle_approval(
        token=token,
        totp_code="",
        user_id="tg_admin",
        session_id="tg_session_x",
    )

    assert "evolution_commit_and_push" in reply
    assert "success" in reply
    assert called["branch"] == "main"
    assert called["user_id"] == "tg_admin"
    assert called["session_id"] == "tg_session_x"


@pytest.mark.asyncio
async def test_handle_approval_refuses_token_for_different_user(
    isolated_pending_db,
):
    from app.core.approval_intercept import handle_approval
    from app.core.pending_actions import stage_action

    token = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_alice", "s"
    )
    reply = await handle_approval(
        token=token, totp_code="", user_id="tg_eve", session_id="s"
    )
    assert "different user" in reply.lower() or "refused" in reply.lower()


@pytest.mark.asyncio
async def test_handle_approval_consumes_token_only_once(
    isolated_pending_db, monkeypatch
):
    """``get_and_delete_action`` removes the row on read. A second
    approval attempt on the same token must fail."""
    from app.core import approval_intercept
    from app.core.pending_actions import stage_action

    def fake_tool(branch: str, tool_context=None):
        return {"status": "success"}

    import app.tools as tools_module

    monkeypatch.setattr(tools_module, "evolution_commit_and_push", fake_tool, raising=False)

    token = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_admin", "s"
    )

    first = await approval_intercept.handle_approval(
        token=token, totp_code="", user_id="tg_admin", session_id="s"
    )
    assert "success" in first

    second = await approval_intercept.handle_approval(
        token=token, totp_code="", user_id="tg_admin", session_id="s"
    )
    assert "invalid" in second.lower() or "expired" in second.lower()


@pytest.mark.asyncio
async def test_handle_approval_requires_totp_when_configured(
    isolated_pending_db, monkeypatch
):
    """If ``ADMIN_TOTP_SECRET`` is set + ``REQUIRE_2FA=true``, a
    missing totp_code short-circuits BEFORE consuming the staged
    action (otherwise the action would be lost)."""
    from app.core.approval_intercept import handle_approval
    from app.core.pending_actions import stage_action

    monkeypatch.setenv("ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("REQUIRE_2FA", "true")

    token = stage_action(
        "evolution_commit_and_push", {"branch": "main"}, "tg_admin", "s"
    )

    reply = await handle_approval(
        token=token, totp_code="", user_id="tg_admin", session_id="s"
    )
    assert "2fa" in reply.lower()

    # Token must still be there — verify by trying again w/ valid TOTP
    # mock.
    from app.core import approval_intercept as ai_mod

    monkeypatch.setattr(
        "app.app_utils.totp.verify_totp", lambda secret, code: True
    )
    import app.tools as tools_module

    def fake_tool(branch: str, tool_context=None):
        return {"status": "success"}

    monkeypatch.setattr(tools_module, "evolution_commit_and_push", fake_tool, raising=False)

    reply2 = await ai_mod.handle_approval(
        token=token, totp_code="123456", user_id="tg_admin", session_id="s"
    )
    assert "success" in reply2
