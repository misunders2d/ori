import pytest
import os
from unittest.mock import MagicMock
from app.callbacks.guardrails import admin_tool_guardrail

def test_admin_tool_guardrail_stages_for_admin():
    """Verifies that the admin_tool_guardrail stages system tools for admins."""
    
    # Mock OS environ for ADMIN_USER_IDS
    os.environ["ADMIN_USER_IDS"] = "tg_123"
    
    tools_to_test = [
        "session_refresh",
        "update_self",
        "trigger_rollback",
        "set_planner_mode",
        "run_system_task_now"
    ]
    
    for tool_name in tools_to_test:
        mock_tool_call = MagicMock()
        mock_tool_call.name = tool_name
        
        args = {"test": "args"}
        
        mock_callback_context = MagicMock()
        mock_callback_context.state.to_dict.return_value = {"user_id": "tg_123", "session_id": "sess1"}
        
        # Call the guardrail with the correct ADK signature
        result = admin_tool_guardrail(tool=mock_tool_call, args=args, tool_context=mock_callback_context)
        
        assert result is not None, f"Admin guardrail should return staging message for {tool_name}"
        assert result["status"] == "error"
        assert "CRITICAL ACTION STAGED" in result["message"]
        assert "Approve ACT-" in result["message"]

def test_admin_tool_guardrail_blocks_new_tools_for_non_admin():
    """Verifies that the admin_tool_guardrail blocks the new system tools for non-admins."""
    
    os.environ["ADMIN_USER_IDS"] = "tg_123"
    
    tools_to_test = [
        "session_refresh",
        "update_self",
        "trigger_rollback",
        "set_planner_mode"
    ]
    
    for tool_name in tools_to_test:
        mock_tool_call = MagicMock()
        mock_tool_call.name = tool_name
        
        args = {}
        
        mock_callback_context = MagicMock()
        mock_callback_context.state.to_dict.return_value = {"user_id": "attacker_456"}
        
        # Call the guardrail with the correct ADK signature
        result = admin_tool_guardrail(tool=mock_tool_call, args=args, tool_context=mock_callback_context)
        
        assert result is not None
        assert result["status"] == "error"
        assert "Only Admin/Master users can invoke" in result["message"]

def test_session_refresh_uses_string_session_id():
    """Verifies that session_refresh now correctly pulls session_id (the string ID)."""
    from unittest.mock import patch
    
    mock_session = MagicMock()
    # If session.session_id is present, it should use it.
    mock_session.session_id = "tg_chat_330959414"
    mock_session.id = 1 # The internal integer ID it used to use
    
    mock_tool_context = MagicMock()
    mock_tool_context.session = mock_session
    
    with patch("app.session_signals.request_refresh") as mock_request_refresh:
        # We need to mock get_pending_refresh to prevent side effects or just ignore it
        from app.tools.system import session_refresh
        
        result = session_refresh(mode="fresh", tool_context=mock_tool_context)
        
        assert result["status"] == "success"
        # Verify request_refresh was called with the STRING id, not the integer 1
        mock_request_refresh.assert_called_once_with("tg_chat_330959414", "fresh")

@pytest.mark.parametrize("tool_name", ["update_self", "trigger_rollback"])
def test_system_tools_use_string_session_id_for_notifications(tool_name):
    """Verifies that system tools use the string session_id for notification routing."""
    from app.tools import system
    from unittest.mock import patch
    tool_func = getattr(system, tool_name)
    
    mock_session = MagicMock()
    mock_session.session_id = "tg_chat_999"
    mock_session.id = 42
    
    mock_tool_context = MagicMock()
    mock_tool_context.session = mock_session
    
    with patch("app.core.transport.parse_notify_from_session_id") as mock_parse:
        # We don't want to actually write trigger files
        with patch("builtins.open", MagicMock()):
            tool_func(tool_context=mock_tool_context)
            # Verify parse_notify_from_session_id was called with the STRING id
            mock_parse.assert_called_once_with("tg_chat_999")
