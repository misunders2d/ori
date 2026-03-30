import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from google.genai import types
from app.core.agent_executor import extract_agent_response, AgentResponse

@pytest.mark.asyncio
async def test_yes_response_creates_function_response_for_pending_confirmation():
    """Verifies that 'yes' is detected as a confirmation and converted to a FunctionResponse,
    NOT forwarded as a new chat message (which would cause an infinite confirmation loop)."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    # Build a session with a pending confirmation in event history
    mock_confirmation = MagicMock()
    mock_confirmation.hint = "Deploy latest code"

    past_event = MagicMock()
    past_event.actions = MagicMock()
    past_event.actions.requested_tool_confirmations = {"call_abc": mock_confirmation}

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = [past_event]
    session.state = {"_confirmation_owner": "tg_123"}
    runner.session_service.get_session.return_value = session
    runner.session_service.append_event = AsyncMock()

    # The runner should receive a FunctionResponse, not a text message.
    # Track what message_arg is passed to run_async.
    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
        # Yield a simple text response so the function completes
        text_event = MagicMock()
        text_event.content = MagicMock()
        text_part = MagicMock()
        text_part.text = "Update started."
        text_part.inline_data = None
        text_event.content.parts = [text_part]
        text_event.actions = None
        text_event.get_function_calls = MagicMock(return_value=[])
        yield text_event

    runner.run_async = mock_run_async

    # User sends "yes" as a types.Content (as the Telegram poller would)
    yes_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): yes")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", yes_message, actual_caller_id="tg_123")

    # The critical assertion: run_async must have received a FunctionResponse, not a text "yes"
    new_message = captured_args.get("new_message")
    assert new_message is not None, "run_async was not called"
    assert new_message.parts, "message_arg has no parts"

    fr_part = new_message.parts[0]
    assert hasattr(fr_part, "function_response") and fr_part.function_response, \
        f"Expected FunctionResponse part, got: {fr_part}"
    assert fr_part.function_response.id == "call_abc"
    assert fr_part.function_response.response["confirmed"] is True


@pytest.mark.asyncio
async def test_no_response_denies_pending_confirmation():
    """Verifies that 'no' correctly denies a pending confirmation."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    mock_confirmation = MagicMock()
    past_event = MagicMock()
    past_event.actions = MagicMock()
    past_event.actions.requested_tool_confirmations = {"call_xyz": mock_confirmation}

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = [past_event]
    session.state = {"_confirmation_owner": "tg_123"}
    runner.session_service.get_session.return_value = session
    runner.session_service.append_event = AsyncMock()

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
        text_event = MagicMock()
        text_event.content = MagicMock()
        text_part = MagicMock()
        text_part.text = "Cancelled."
        text_part.inline_data = None
        text_event.content.parts = [text_part]
        text_event.actions = None
        text_event.get_function_calls = MagicMock(return_value=[])
        yield text_event

    runner.run_async = mock_run_async

    no_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): no")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", no_message, actual_caller_id="tg_123")

    new_message = captured_args.get("new_message")
    fr_part = new_message.parts[0]
    assert fr_part.function_response.response["confirmed"] is False


@pytest.mark.asyncio
async def test_different_user_cannot_confirm_in_group_chat():
    """In a group chat, only the user who triggered the action can confirm it."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    mock_confirmation = MagicMock()
    past_event = MagicMock()
    past_event.actions = MagicMock()
    past_event.actions.requested_tool_confirmations = {"call_group": mock_confirmation}

    session = MagicMock()
    session.id = "tg_group_456"
    session.events = [past_event]
    session.state = {"_confirmation_owner": "tg_111"}  # User A owns the confirmation
    runner.session_service.get_session.return_value = session
    runner.session_service.append_event = AsyncMock()

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
        text_event = MagicMock()
        text_event.content = MagicMock()
        text_part = MagicMock()
        text_part.text = "ok"
        text_part.inline_data = None
        text_event.content.parts = [text_part]
        text_event.actions = None
        text_event.get_function_calls = MagicMock(return_value=[])
        yield text_event

    runner.run_async = mock_run_async

    # User B (tg_222) tries to confirm User A's action
    yes_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Bob (tg_222): yes")]
    )
    response = await extract_agent_response(runner, "tg_222", "tg_group_456", yes_message, actual_caller_id="tg_222")

    # The message should be forwarded as plain text, NOT as a FunctionResponse
    new_message = captured_args.get("new_message")
    assert new_message is not None
    first_part = new_message.parts[0]
    # Should be a text part, not a function_response
    assert hasattr(first_part, "text") and first_part.text, \
        f"Expected plain text (rejected confirmation), got: {first_part}"
    assert first_part.function_response is None


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

@pytest.mark.asyncio
async def test_extract_agent_response_refresh_confirmation_reason():
    """Verifies that common tools get specific human-readable reasons if hint is missing."""
    
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    # Session state with _confirmation_reason set by the before_tool_callback
    mock_session = MagicMock()
    mock_session.events = []
    mock_session.state = {"_confirmation_reason": "Clear conversation history (mode: summarize)"}
    runner.session_service.get_session.return_value = mock_session

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

    # Assert reason from session state (set by confirmation_reason_callback)
    assert "📋 **Reason:** Clear conversation history (mode: summarize)" in response.text

@pytest.mark.asyncio
async def test_extract_agent_response_evolution_confirmation_reason():
    """Verifies that evolution tool gets specific human-readable reasons."""
    
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    # Session state with _confirmation_reason set by the before_tool_callback
    mock_session = MagicMock()
    mock_session.events = []
    mock_session.state = {"_confirmation_reason": "Commit and push: Add tests"}
    runner.session_service.get_session.return_value = mock_session

    mock_confirmation = MagicMock()
    mock_confirmation.hint = ""
    mock_confirmation.payload = {"tool_context": {}, "commit_message": "Add tests"}

    mock_event = MagicMock()
    mock_event.author = "DeveloperAgent"
    mock_event.content = None
    mock_event.actions = MagicMock()
    mock_event.actions.requested_tool_confirmations = {
        "call_789": mock_confirmation
    }

    mock_fc = MagicMock()
    mock_fc.id = "call_789"
    mock_fc.name = "evolution_commit_and_push"
    mock_event.get_function_calls.return_value = [mock_fc]

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "user_id", "session_id", "message")

    # Assert reason from session state (set by confirmation_reason_callback)
    assert "📋 **Reason:** Commit and push: Add tests" in response.text
