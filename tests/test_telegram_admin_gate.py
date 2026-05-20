"""Slice 10 — telegram_grant_capability + telegram_revoke_capability
must flow through admin_tool_guardrail's ACT+TOTP-staged list. Mirrors
the existing test pattern from `tests/test_2fa_toggle.py`."""

from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock, patch

from app.callbacks.guardrails import admin_tool_guardrail


class _MockTool:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def admin_ctx():
    ctx = MagicMock()
    ctx.state.to_dict.return_value = {
        "user_id": "admin_user",
        "session_id": "test_session",
    }
    return ctx


@pytest.fixture
def nonadmin_ctx():
    ctx = MagicMock()
    ctx.state.to_dict.return_value = {
        "user_id": "tg_222",
        "session_id": "test_session",
    }
    return ctx


@pytest.mark.parametrize(
    "tool_name", ["telegram_grant_capability", "telegram_revoke_capability"]
)
def test_admin_caller_stages_act_token(admin_ctx, tool_name):
    """Admin invocation → ACT-token staged; tool execution aborted with
    a status:error that contains an `Approve <token>` instruction."""
    tool = _MockTool(tool_name)
    with patch.dict(
        os.environ,
        {"ADMIN_USER_IDS": "admin_user", "REQUIRE_2FA": "false"},
    ), patch(
        "app.core.pending_actions.stage_action", return_value="ACT-TEST",
    ):
        result = admin_tool_guardrail(tool, {}, admin_ctx)
    assert result is not None
    assert result["status"] == "error"
    assert "Approve ACT-TEST" in result["message"]


@pytest.mark.parametrize(
    "tool_name", ["telegram_grant_capability", "telegram_revoke_capability"]
)
def test_admin_caller_stages_with_2fa_when_enabled(admin_ctx, tool_name):
    tool = _MockTool(tool_name)
    with patch.dict(
        os.environ,
        {
            "ADMIN_USER_IDS": "admin_user",
            "ADMIN_TOTP_SECRET": "base32secret3232",
            "REQUIRE_2FA": "true",
        },
    ), patch(
        "app.core.pending_actions.stage_action", return_value="ACT-TEST",
    ):
        result = admin_tool_guardrail(tool, {}, admin_ctx)
    assert "2FA code" in result["message"]
    assert "Approve ACT-TEST" in result["message"]


@pytest.mark.parametrize(
    "tool_name", ["telegram_grant_capability", "telegram_revoke_capability"]
)
def test_nonadmin_blocked_outright(nonadmin_ctx, tool_name):
    """Non-admin → blocked with `Unauthorized` status:error, no
    staging."""
    tool = _MockTool(tool_name)
    with patch.dict(
        os.environ, {"ADMIN_USER_IDS": "admin_user", "REQUIRE_2FA": "false"}
    ):
        result = admin_tool_guardrail(tool, {}, nonadmin_ctx)
    assert result is not None
    assert result["status"] == "error"
    assert "Only Admin/Master" in result["message"]


def test_other_telegram_tools_are_not_gated(admin_ctx):
    """Read-only / self-scope telegram tools must NOT trigger the ACT
    flow — only the two mutation tools are gated."""
    # 12 total telegram tools minus the 2 gated ones = 10 non-gated.
    for name in (
        "telegram_send_dm",
        "telegram_send_to_chat",
        "telegram_save_alias",
        "telegram_save_last_forward_alias",
        "telegram_list_aliases",
        "telegram_delete_alias",
        "telegram_resolve_alias",
        "telegram_forward",
        "telegram_list_cached_files",
        "telegram_list_capabilities",
    ):
        tool = _MockTool(name)
        with patch.dict(
            os.environ,
            {"ADMIN_USER_IDS": "admin_user", "REQUIRE_2FA": "false"},
        ):
            result = admin_tool_guardrail(tool, {}, admin_ctx)
        assert result is None, f"{name} unexpectedly gated"
