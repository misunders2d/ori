import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from google.genai import types
from app.core.agent_executor import extract_agent_response, AgentResponse

@pytest.mark.asyncio
async def test_extract_agent_response_confirmation_formatting():
    """Verifies that tool confirmation messages are formatted with agent name and reasons."""
    
    # Mock Runner and Session
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    
    session = MagicMock()
    session.id = "tg_chat_123"
    session.user_id = "tg_123"
    session.events = []
    
    runner.session_service.get_session.return_value = session
    
    # Mock a ToolConfirmation action from ADK
    mock_confirmation = MagicMock()
    mock_confirmation.hint = "Update the bot to the latest version."
    mock_confirmation.payload = {"tool_context": {}, "mode": "fresh"}
    
    # Mock an event with tool confirmation actions
    mock_event = MagicMock()
    mock_event.author = "CoordinatorAgent"
    mock_event.content = None
    mock_event.actions = MagicMock()
    mock_event.actions.requested_tool_confirmations = {
        "call_123": mock_confirmation
    }
    
    mock_fc = MagicMock()
    mock_fc.id = "call_123"
    mock_fc.name = "update_self"
    mock_event.get_function_calls.return_value = [mock_fc]
    
    # Define the mock generator for run_async
    async def mock_run_async(*args, **kwargs):
        yield mock_event
        
    runner.run_async = mock_run_async
    
    # Execute
    response = await extract_agent_response(runner, "user_id", "session_id", "message")
    
    # Assert formatting
    assert "⚠️ **Action Requires Confirmation**" in response.text
    assert "**CoordinatorAgent** wants to execute `update_self`" in response.text
    assert "📋 **Reason:** Update the bot to the latest version." in response.text
    assert "approve or deny" in response.text

@pytest.mark.asyncio
async def test_extract_agent_response_refresh_confirmation_reason():
    """Verifies that common tools get specific human-readable reasons if hint is missing."""
    
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = MagicMock(events=[])
    
    mock_confirmation = MagicMock()
    mock_confirmation.hint = "" # Missing hint
    mock_confirmation.payload = {"tool_context": {}, "mode": "summarize"}
    
    mock_event = MagicMock()
    mock_event.author = "CoordinatorAgent"
    mock_event.content = None
    mock_event.actions = MagicMock()
    mock_event.actions.requested_tool_confirmations = {
        "call_456": mock_confirmation
    }
    
    mock_fc = MagicMock()
    mock_fc.id = "call_456"
    mock_fc.name = "session_refresh"
    mock_event.get_function_calls.return_value = [mock_fc]
    
    async def mock_run_async(*args, **kwargs):
        yield mock_event
        
    runner.run_async = mock_run_async
    
    response = await extract_agent_response(runner, "user_id", "session_id", "message")
    
    # Assert generated reason
    assert "📋 **Reason:** Clear conversation history (Mode: summarize)." in response.text
