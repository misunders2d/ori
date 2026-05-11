import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from google.genai import types
from app.core.agent_executor import extract_agent_response, AgentResponse


@pytest.mark.asyncio
async def test_extract_agent_response_returns_text():
    """Verifies that extract_agent_response returns text from the agent."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        text_event = MagicMock()
        text_event.content = MagicMock()
        text_part = MagicMock()
        text_part.text = "Hello, world!"
        text_part.inline_data = None
        text_event.content.parts = [text_part]
        text_event.actions = None
        yield text_event

    runner.run_async = mock_run_async

    msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text="Message from Sergey (tg_123): hi")]
    )
    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", msg)

    assert response.text == "Hello, world!"
    assert response.media_items == []


@pytest.mark.asyncio
async def test_extract_agent_response_no_text_fallback():
    """Verifies fallback message when agent produces no text."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        event = MagicMock()
        event.content = MagicMock()
        event.content.parts = []
        event.actions = None
        yield event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", "hello")

    assert response.text == "I processed your request but have no response to show."


@pytest.mark.asyncio
async def test_extract_agent_response_filters_thought_parts():
    """Reasoning / thinking parts (Anthropic ``thinking_blocks`` normalised
    into ``Part(text=..., thought=True)`` by ADK's LiteLlm wrapper) must
    NOT surface in user-facing channels. ``extract_agent_response`` is the
    single chokepoint for Slack/Telegram/A2A output, so the filter lives
    there. Belt-and-suspenders alongside Gemini's ``thinking_config = None``
    in ``state_setter``.
    """
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    session = MagicMock()
    session.id = "tg_chat_thoughts"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        event = MagicMock()
        event.content = MagicMock()

        thinking_part = MagicMock()
        thinking_part.text = "Let me think... the user wants X, so I should Y."
        thinking_part.thought = True
        thinking_part.inline_data = None

        answer_part = MagicMock()
        answer_part.text = "Here is the answer."
        answer_part.thought = False
        answer_part.inline_data = None

        event.content.parts = [thinking_part, answer_part]
        event.actions = None
        yield event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "tg_123", "tg_chat_thoughts", "hi")

    assert "Let me think" not in response.text, (
        "thinking parts leaked into user-facing response — "
        "extract_agent_response must filter Part(thought=True)"
    )
    assert response.text == "Here is the answer."


@pytest.mark.asyncio
async def test_extract_agent_response_string_message():
    """Verifies that a plain string message is wrapped into Content correctly."""

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()

    session = MagicMock()
    session.id = "tg_chat_123"
    session.events = []
    runner.session_service.get_session.return_value = session

    captured_args = {}

    async def mock_run_async(*args, **kwargs):
        captured_args.update(kwargs)
        text_event = MagicMock()
        text_event.content = MagicMock()
        text_part = MagicMock()
        text_part.text = "Got it."
        text_part.inline_data = None
        text_event.content.parts = [text_part]
        text_event.actions = None
        yield text_event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "tg_123", "tg_chat_123", "test message")

    new_message = captured_args.get("new_message")
    assert new_message is not None
    assert new_message.role == "user"
    assert any(hasattr(p, "text") and p.text for p in new_message.parts)
