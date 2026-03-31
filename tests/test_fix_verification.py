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
