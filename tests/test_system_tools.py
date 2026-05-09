import pytest
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

@pytest.mark.asyncio
async def test_execute_approved_action_invalid_token():
    from app.tools.system import execute_approved_action
    
    # Use patch.dict to avoid polluting real environment and ensure consistency
    with patch.dict(os.environ, {"REQUIRE_2FA": "true"}, clear=False):
        if "ADMIN_TOTP_SECRET" in os.environ:
            del os.environ["ADMIN_TOTP_SECRET"]
            
        with patch("app.core.pending_actions.get_and_delete_action", return_value=None):
            result = await execute_approved_action("BAD-TOKEN")
            assert result["status"] == "error"
            assert "Invalid token" in result["message"]

@pytest.mark.asyncio
async def test_execute_approved_action_totp_required_but_missing():
    from app.tools.system import execute_approved_action
    
    with patch.dict(os.environ, {"ADMIN_TOTP_SECRET": "A" * 16, "REQUIRE_2FA": "true"}, clear=False):
        result = await execute_approved_action("ACT-VALID")
        assert result["status"] == "error"
        assert "Invalid 2FA code" in result["message"]

@pytest.mark.asyncio
async def test_execute_approved_action_totp_valid():
    from app.tools.system import execute_approved_action
    from app.core.pending_actions import get_and_delete_action
    from app.app_utils.totp import verify_totp
    from app.tools.system import session_refresh
    
    mock_action = {
        "tool_name": "session_refresh",
        "args": {"mode": "fresh"}
    }
    
    with patch.dict(os.environ, {"ADMIN_TOTP_SECRET": "A" * 16, "REQUIRE_2FA": "true"}, clear=False):
        with patch("app.app_utils.totp.verify_totp", return_value=True):
            with patch("app.core.pending_actions.get_and_delete_action", return_value=mock_action):
                result = await execute_approved_action("ACT-VALID", totp_code="123456", tool_context=MagicMock())
                assert result["status"] == "success"
                assert "Session refresh" in result["message"]

def test_check_active_tasks():
    from app.tools.diagnostics import check_active_tasks
    from app.tasks import ACTIVE_TASKS
    
    ACTIVE_TASKS.clear()

    result = check_active_tasks(tool_context=MagicMock())
    assert result["status"] == "success"
    assert "No active tasks" in result["message"]
    
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


def test_lifecycle_tool_docstrings_disambiguate_reset_targets():
    from app.tools.system import session_refresh, trigger_rollback, update_self

    update_doc = update_self.__doc__ or ""
    refresh_doc = session_refresh.__doc__ or ""
    rollback_doc = trigger_rollback.__doc__ or ""

    assert "Real process restart/reboot" in update_doc
    assert "Do NOT use for conversation reset" in update_doc
    assert "This does NOT restart the running process" in refresh_doc
    assert "ask" in refresh_doc
    assert "session refresh or process restart" in refresh_doc
    assert "Rollback code to previous commit" in rollback_doc


def test_coordinator_instruction_requires_reset_clarification():
    source = Path("app/sub_agents/coordinator_agent.py").read_text()

    assert "RESET / RESTART AMBIGUITY" in source
    assert "ask which operation they mean" in source
    assert "BEFORE calling any tool" in source
    assert "Never infer this from context" in source
    assert "EXACT LIFECYCLE TOOL REQUESTS" in source
    assert "do not substitute" in source


@pytest.mark.asyncio
async def test_system_toolset_exposes_update_self_in_child_mode():
    from app.toolsets.system import SystemToolset

    with patch("app.toolsets.system._is_child_container", return_value=True):
        tools = await SystemToolset().get_tools()

    tool_names = {getattr(tool, "name", "") for tool in tools}
    assert "update_self" in tool_names
    assert "trigger_rollback" not in tool_names


def test_transport_bare_reset_asks_for_target():
    telegram_source = Path("interfaces/telegram_poller.py").read_text()
    slack_source = Path("interfaces/slack_poller.py").read_text()

    assert 'text.strip() == "/reset"' in telegram_source
    assert "Which reset do you mean?" in telegram_source
    assert 'text.strip() == "/reset session"' in telegram_source
    assert 'command_text != "session"' in slack_source
    assert "_clean_text.lower() == \"reset\"" in slack_source
    assert "Which reset do you mean?" in slack_source
