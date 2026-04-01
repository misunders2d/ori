import pytest
import os
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_execute_approved_action_invalid_token():
    from app.tools.system import execute_approved_action
    
    # Use a clean environment to avoid interference from ADMIN_TOTP_SECRET
    with patch.dict(os.environ, {}, clear=False):
        if "ADMIN_TOTP_SECRET" in os.environ:
            del os.environ["ADMIN_TOTP_SECRET"]
            
        with patch("app.core.pending_actions.get_and_delete_action", return_value=None):
            result = await execute_approved_action("BAD-TOKEN")
            assert result["status"] == "error"
            assert "Invalid or expired" in result["message"]

@pytest.mark.asyncio
async def test_execute_approved_action_totp_required_but_missing():
    from app.tools.system import execute_approved_action
    
    os.environ["ADMIN_TOTP_SECRET"] = "A" * 16
    result = await execute_approved_action("ACT-VALID")
    assert result["status"] == "error"
    assert "Invalid or missing TOTP 2FA code" in result["message"]
    del os.environ["ADMIN_TOTP_SECRET"]

@pytest.mark.asyncio
async def test_execute_approved_action_totp_valid():
    from app.tools.system import execute_approved_action
    from app.core.pending_actions import get_and_delete_action
    from app.app_utils.totp import verify_totp
    from app.tools.system import session_refresh
    
    os.environ["ADMIN_TOTP_SECRET"] = "A" * 16
    
    mock_action = {
        "tool_name": "session_refresh",
        "args": {"mode": "fresh"}
    }
    
    with patch("app.app_utils.totp.verify_totp", return_value=True):
        with patch("app.core.pending_actions.get_and_delete_action", return_value=mock_action):
            result = await execute_approved_action("ACT-VALID", totp_code="123456", tool_context=MagicMock())
            assert result["status"] == "success"
            assert "Session refreshed" in result["message"]
            
    del os.environ["ADMIN_TOTP_SECRET"]

def test_check_active_tasks():
    from app.tools.system import check_active_tasks
    from app.tasks import ACTIVE_TASKS
    
    # Ensure registry is empty for this test
    ACTIVE_TASKS.clear()
    
    result = check_active_tasks(tool_context=MagicMock())
    assert result["status"] == "success"
    assert "no background tasks currently being tracked" in result["message"]
    
    # Add a mock task
    ACTIVE_TASKS["mock_id"] = {
        "prompt": "mock prompt",
        "type": "system",
        "status": "Running",
        "start_time": "2026-03-31T00:00:00",
        "end_time": None,
        "error": None
    }
    
    result = check_active_tasks(tool_context=MagicMock())
    assert result["status"] == "success"
    assert "active_tasks" in result
    assert len(result["active_tasks"]) == 1
    assert result["active_tasks"][0]["task_id"] == "mock_id"
    
    ACTIVE_TASKS.clear()