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
    runner.session_service.get_session.return_value = session

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
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

    yes_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): yes")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", yes_message)

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
    runner.session_service.get_session.return_value = session

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
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", no_message)

    new_message = captured_args.get("new_message")
    fr_part = new_message.parts[0]
    assert fr_part.function_response.response["confirmed"] is False


@pytest.mark.asyncio
async def test_extract_agent_response_confirmation_formatting():
    """Verifies that tool confirmation messages are formatted with agent name and reasons."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    session = MagicMock()
    session.id = "tg_chat_123"
    session.user_id = "tg_123"
    session.events = []

    runner.session_service.get_session.return_value = session

    mock_confirmation = MagicMock()
    mock_confirmation.hint = "Update the bot to the latest version."
    mock_confirmation.payload = {"tool_context": {}, "mode": "fresh"}

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

    async def mock_run_async(*args, **kwargs):
        yield mock_event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "user_id", "session_id", "message")

    assert "⚠️ **Action Requires Confirmation**" in response.text
    assert "**CoordinatorAgent** wants to execute `update_self`" in response.text
    assert "📋 **Reason:** Update the bot to the latest version." in response.text

@pytest.mark.asyncio
async def test_extract_agent_response_refresh_confirmation_reason():
    """Verifies that common tools get specific human-readable reasons if hint is missing."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = MagicMock(events=[])

    mock_confirmation = MagicMock()
    mock_confirmation.hint = ""
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

    assert "📋 **Reason:** Clear conversation history (mode: summarize)." in response.text

@pytest.mark.asyncio
async def test_extract_agent_response_evolution_confirmation_reason():
    """Verifies that evolution tool gets specific human-readable reasons."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = MagicMock(events=[])

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

    assert "📋 **Reason:** Commit and push: Add tests" in response.text


@pytest.mark.asyncio
async def test_natural_language_confirmation_accepted():
    """Verifies that 'of course', 'sure', etc. are recognized as confirmations."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    mock_confirmation = MagicMock()
    past_event = MagicMock()
    past_event.actions = MagicMock()
    past_event.actions.requested_tool_confirmations = {"call_nat": mock_confirmation}
    past_event.content = None

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = [past_event]
    runner.session_service.get_session.return_value = session

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
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

    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): of course")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", msg)

    new_message = captured_args.get("new_message")
    assert new_message is not None
    fr_part = new_message.parts[0]
    assert fr_part.function_response.id == "call_nat"
    assert fr_part.function_response.response["confirmed"] is True


@pytest.mark.asyncio
async def test_already_confirmed_call_ids_are_skipped():
    """Verifies that confirmations already responded to are not re-matched."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    mock_confirmation = MagicMock()

    # Event 1: old confirmation request (already responded to)
    old_confirm_event = MagicMock()
    old_confirm_event.actions = MagicMock()
    old_confirm_event.actions.requested_tool_confirmations = {"call_old": mock_confirmation}
    old_confirm_event.content = None

    # Event 2: user's FunctionResponse that already confirmed the old request
    old_response_event = MagicMock()
    old_response_event.actions = None
    old_fr = MagicMock()
    old_fr.name = "adk_request_confirmation"
    old_fr.id = "call_old"
    old_fr_part = MagicMock()
    old_fr_part.function_response = old_fr
    old_response_event.content = MagicMock()
    old_response_event.content.parts = [old_fr_part]

    # Event 3: agent text response (LLM asking for more confirmation — the bug)
    text_event = MagicMock()
    text_event.actions = None
    text_event.content = MagicMock()
    text_part = MagicMock()
    text_part.text = "Please confirm the reboot."
    text_part.function_response = None
    text_event.content.parts = [text_part]

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = [old_confirm_event, old_response_event, text_event]
    runner.session_service.get_session.return_value = session

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
        resp_event = MagicMock()
        resp_event.content = MagicMock()
        resp_part = MagicMock()
        resp_part.text = "Ok."
        resp_part.inline_data = None
        resp_event.content.parts = [resp_part]
        resp_event.actions = None
        resp_event.get_function_calls = MagicMock(return_value=[])
        yield resp_event

    runner.run_async = mock_run_async

    # User says "yes" again, but the old confirmation is already resolved
    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): yes")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", msg)

    # Should be sent as a regular message, NOT as a FunctionResponse
    new_message = captured_args.get("new_message")
    assert new_message is not None
    # The message should be the original Content (not a FunctionResponse)
    has_fr = any(
        hasattr(p, "function_response") and p.function_response
        for p in new_message.parts
    )
    assert not has_fr, "Stale confirmation was re-matched — deduplication failed"
