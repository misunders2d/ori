import os
import sys
# Ensure the staged app is picked up first
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from unittest.mock import MagicMock, patch
from app.callbacks.guardrails import admin_tool_guardrail
from app.tools.system import execute_approved_action

class MockTool:
    def __init__(self, name):
        self.name = name

@pytest.fixture
def mock_tool_context():
    context = MagicMock()
    context.state.to_dict.return_value = {
        "user_id": "admin_user",
        "session_id": "test_session"
    }
    return context

@pytest.mark.asyncio
async def test_admin_guardrail_respects_require_2fa_toggle(mock_tool_context):
    tool = MockTool("update_self")
    
    # CASE: REQUIRE_2FA=false
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "admin_user", "ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "false"}):
        with patch("app.core.pending_actions.stage_action", return_value="test_token"):
            result = admin_tool_guardrail(tool, {}, mock_tool_context)
            assert result["status"] == "error"
            assert "Approve test_token" in result["message"]
            assert "2FA code" not in result["message"]

    # CASE: REQUIRE_2FA=true (default)
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "admin_user", "ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "true"}):
        with patch("app.core.pending_actions.stage_action", return_value="test_token"):
            result = admin_tool_guardrail(tool, {}, mock_tool_context)
            assert "2FA code" in result["message"]

@pytest.mark.asyncio
async def test_execute_approved_action_respects_require_2fa_toggle():
    # CASE: REQUIRE_2FA=false (Execution without code)
    with patch.dict(os.environ, {"ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "false"}):
        with patch("app.core.pending_actions.get_and_delete_action", return_value={"tool_name": "session_refresh", "args": {"mode": "soft"}}):
            with patch("app.tools.session_refresh", return_value={"status": "success"}):
                result = await execute_approved_action("test_token")
                assert result["status"] == "success"

    # CASE: REQUIRE_2FA=true (Execution without code fails)
    with patch.dict(os.environ, {"ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "true"}):
        # Should fail without totp_code
        result = await execute_approved_action("test_token")
        assert result["status"] == "error"
        assert "Invalid 2FA code" in result["message"]
