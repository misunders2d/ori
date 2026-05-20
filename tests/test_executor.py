import os

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
async def test_extract_agent_response_dedupes_inline_data_against_function_response(
    tmp_path,
):
    """The file_attachment_inject callback adds an inline_data Part with
    a ``__contract_file:<path>`` display_name marker so the A2A converter
    ships the file as a FilePart. The same file is ALSO reachable via the
    function_response.file_path branch (Slack/Telegram's historical
    route). Without dedup, the Slack/Telegram poller would attach the
    file twice. This test pins the dedup logic.
    """
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\nbytes")

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    session = MagicMock()
    session.id = "sl_test"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        # 1) tool returned file_path (the legacy Slack/Telegram route)
        fr_part = MagicMock()
        fr_part.text = None
        fr_part.inline_data = None
        fr_part.function_response = MagicMock()
        fr_part.function_response.response = {
            "status": "success",
            "file_path": str(chart),
        }
        fr_event = MagicMock()
        fr_event.content = MagicMock()
        fr_event.content.parts = [fr_part]
        fr_event.actions = None
        yield fr_event

        # 2) model response WITH inline_data marker (the new A2A route)
        text_part = MagicMock()
        text_part.text = "Chart attached."
        text_part.thought = False
        text_part.inline_data = None
        text_part.function_response = None

        inline_part = MagicMock()
        inline_part.text = None
        inline_part.thought = False
        inline_part.function_response = None
        inline_part.inline_data = MagicMock()
        inline_part.inline_data.data = b"\x89PNG\r\n\x1a\nbytes"
        inline_part.inline_data.mime_type = "image/png"
        inline_part.inline_data.display_name = f"__contract_file:{os.path.abspath(str(chart))}"

        model_event = MagicMock()
        model_event.content = MagicMock()
        model_event.content.parts = [text_part, inline_part]
        model_event.actions = None
        yield model_event

    runner.run_async = mock_run_async

    response = await extract_agent_response(runner, "tg_123", "sl_test", "go")

    # File present exactly once — neither leg double-attached.
    assert len(response.media_items) == 1, (
        f"expected one attachment, got {len(response.media_items)} — "
        "inline_data + function_response should be deduped on path marker"
    )
    assert response.media_items[0]["mime_type"] == "image/png"


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


# ---------------------------------------------------------------------------
# slice 5: media_items[*]["file_path"] is threaded so the transport layer
# can auto-derive a file_ref for the Telegram outbound-files cache.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_function_response_branch_includes_file_path(tmp_path):
    """Tool emits a `file_path`; the produced media_item must carry it
    so slice-6's poller delivery loop can pass it into
    `send_media_strict(file_path=..., owner_user_id=...)`."""
    chart = tmp_path / "q1_revenue.png"
    chart.write_bytes(b"\x89PNGbytes")

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    session = MagicMock()
    session.id = "tg_chat_x"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        fr_part = MagicMock()
        fr_part.text = None
        fr_part.inline_data = None
        fr_part.function_response = MagicMock()
        fr_part.function_response.response = {
            "status": "success",
            "file_path": str(chart),
        }
        event = MagicMock()
        event.content = MagicMock()
        event.content.parts = [fr_part]
        event.actions = None
        yield event

    runner.run_async = mock_run_async
    response = await extract_agent_response(runner, "tg_123", "tg_chat_x", "go")

    assert len(response.media_items) == 1
    item = response.media_items[0]
    assert item["mime_type"] == "image/png"
    assert item["file_path"] == os.path.abspath(str(chart))


@pytest.mark.asyncio
async def test_inline_data_branch_includes_marker_path(tmp_path):
    """`file_attachment_inject` adds an inline_data Part with the
    `__contract_file:<abs path>` display_name marker. The path must
    be exposed as `media_items[i]["file_path"]` so slice 6 can route it
    into the file cache without double-attaching (existing dedup intact).
    """
    chart = tmp_path / "bar.png"
    chart.write_bytes(b"\x89PNGbytes")

    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    session = MagicMock()
    session.id = "tg_chat_x"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        text_part = MagicMock()
        text_part.text = "Chart attached."
        text_part.thought = False
        text_part.inline_data = None
        text_part.function_response = None

        inline_part = MagicMock()
        inline_part.text = None
        inline_part.thought = False
        inline_part.function_response = None
        inline_part.inline_data = MagicMock()
        inline_part.inline_data.data = b"\x89PNGbytes"
        inline_part.inline_data.mime_type = "image/png"
        inline_part.inline_data.display_name = (
            f"__contract_file:{os.path.abspath(str(chart))}"
        )

        event = MagicMock()
        event.content = MagicMock()
        event.content.parts = [text_part, inline_part]
        event.actions = None
        yield event

    runner.run_async = mock_run_async
    response = await extract_agent_response(runner, "tg_123", "tg_chat_x", "go")

    assert len(response.media_items) == 1
    item = response.media_items[0]
    assert item["mime_type"] == "image/png"
    assert item["file_path"] == os.path.abspath(str(chart))


@pytest.mark.asyncio
async def test_inline_data_user_upload_has_none_file_path():
    """A user-uploaded inline_data Part has no `__contract_file:` marker,
    so `file_path` must be None — transport must not write a cache row
    for somebody else's upload."""
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    session = MagicMock()
    session.id = "tg_chat_y"
    session.events = []
    runner.session_service.get_session.return_value = session

    async def mock_run_async(*args, **kwargs):
        inline_part = MagicMock()
        inline_part.text = None
        inline_part.thought = False
        inline_part.function_response = None
        inline_part.inline_data = MagicMock()
        inline_part.inline_data.data = b"USER-UPLOAD"
        inline_part.inline_data.mime_type = "image/jpeg"
        inline_part.inline_data.display_name = ""  # NO marker

        event = MagicMock()
        event.content = MagicMock()
        event.content.parts = [inline_part]
        event.actions = None
        yield event

    runner.run_async = mock_run_async
    response = await extract_agent_response(runner, "tg_123", "tg_chat_y", "ignore")

    assert len(response.media_items) == 1
    assert response.media_items[0]["file_path"] is None
