"""Transport REGISTRY + Telegram adapter — multimodal send_media included.

The poller modules import `app.runtime.executor` (Phase G); they're not
loaded here. Adapter is fully testable.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.transports import TRANSPORTS, list_transports, register_transport
from app.transports.telegram import TelegramAdapter, _scrub_secrets

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_default_registry_order():
    """telegram before cli — messenger has priority over fallback."""
    order = list_transports()
    assert order.index("telegram") < order.index("cli")


def test_register_transport_inserts_before_cli():
    """New transports slot in BEFORE the CLI fallback."""
    initial = list_transports()
    placeholder = "discord"  # not in defaults; safe to register/unregister
    assert placeholder not in initial, "test placeholder collided with a real default"
    try:
        register_transport(placeholder)
        assert placeholder in TRANSPORTS
        assert TRANSPORTS.index(placeholder) < TRANSPORTS.index("cli")
    finally:
        if placeholder in TRANSPORTS:
            TRANSPORTS.remove(placeholder)
    assert list_transports() == initial


def test_register_transport_idempotent():
    """Re-registering the same name is a no-op."""
    initial = list_transports()
    register_transport("telegram")
    assert list_transports() == initial


# ---------------------------------------------------------------------------
# TelegramAdapter — id helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    client = MagicMock()
    return TelegramAdapter(client=client, token="test-token")


def test_platform_name(adapter):
    assert adapter.platform_name == "telegram"


def test_make_session_id(adapter):
    assert adapter.make_session_id(123) == "tg_123"
    assert adapter.make_session_id("987") == "tg_987"


def test_make_user_id(adapter):
    assert adapter.make_user_id(42) == "tg_42"


def test_parse_notify_info(adapter):
    assert adapter.parse_notify_info("tg_42") == {"type": "telegram", "chat_id": 42}
    # Non-tg session ids return empty.
    assert adapter.parse_notify_info("slack_C123") == {}
    # Malformed tg_ session ids return empty (chat_id not numeric).
    assert adapter.parse_notify_info("tg_abc") == {}


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

def test_scrub_secrets_redacts_env_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyExampleSecretValue1234567890")
    text = "Here is your key: AIzaSyExampleSecretValue1234567890 — careful!"
    out = _scrub_secrets(text)
    assert "AIzaSyExampleSecretValue1234567890" not in out
    assert "[REDACTED]" in out


def test_scrub_secrets_catches_token_patterns():
    text = "Use ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa for github"
    out = _scrub_secrets(text)
    assert "ghp_" not in out
    assert "[REDACTED]" in out


def test_scrub_secrets_preserves_normal_text():
    text = "the quick brown fox jumps over the lazy dog"
    assert _scrub_secrets(text) == text


# ---------------------------------------------------------------------------
# send_message (chunking + Markdown fallback)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_message_short(adapter):
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_message(123, "hello")
    _args, kwargs = adapter._client.post.call_args
    assert kwargs["json"]["chat_id"] == 123
    assert kwargs["json"]["text"] == "hello"
    assert kwargs["json"]["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_message_falls_back_to_plain_when_markdown_fails(adapter):
    """Markdown parse error -> retry without parse_mode."""
    bad = MagicMock(status_code=400, text="markdown parse error")
    good = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(side_effect=[bad, good])
    await adapter.send_message(123, "hello [bracket")
    assert adapter._client.post.call_count == 2
    # First call had Markdown; second did not.
    second_call = adapter._client.post.call_args_list[1]
    assert "parse_mode" not in second_call.kwargs["json"]


@pytest.mark.asyncio
async def test_send_message_chunks_long_text(adapter):
    """Anything over 4000 chars is split on newlines."""
    long_text = "line1\n" + ("a" * 3990) + "\nline2\n" + ("b" * 3990) + "\nline3"
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_message(123, long_text)
    # Three logical chunks.
    assert adapter._client.post.call_count == 3


# ---------------------------------------------------------------------------
# send_media — multimodal routing by mime_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_media_image_routes_to_sendPhoto(adapter):
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_media(123, b"png-bytes", "image/png", "caption!")
    url = adapter._client.post.call_args.args[0]
    assert "sendPhoto" in url
    files = adapter._client.post.call_args.kwargs["files"]
    assert "photo" in files


@pytest.mark.asyncio
async def test_send_media_audio_routes_to_sendAudio(adapter):
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_media(123, b"audio-bytes", "audio/mpeg")
    assert "sendAudio" in adapter._client.post.call_args.args[0]
    assert "audio" in adapter._client.post.call_args.kwargs["files"]


@pytest.mark.asyncio
async def test_send_media_video_routes_to_sendVideo(adapter):
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_media(123, b"video-bytes", "video/mp4")
    assert "sendVideo" in adapter._client.post.call_args.args[0]


@pytest.mark.asyncio
async def test_send_media_unknown_mime_routes_to_sendDocument(adapter):
    """application/gzip -> sendDocument (fallback path for arbitrary binaries — important for DNA bundles)."""
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_media(123, b"\x1f\x8b" + b"\x00" * 100, "application/gzip")
    assert "sendDocument" in adapter._client.post.call_args.args[0]
    assert "document" in adapter._client.post.call_args.kwargs["files"]


@pytest.mark.asyncio
async def test_send_media_includes_caption(adapter):
    resp = MagicMock(status_code=200)
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_media(123, b"img", "image/png", "my caption")
    form = adapter._client.post.call_args.kwargs["data"]
    assert form["caption"] == "my caption"


@pytest.mark.asyncio
async def test_send_typing_uses_correct_endpoint(adapter):
    adapter._client.post = AsyncMock()
    await adapter.send_typing(123)
    url = adapter._client.post.call_args.args[0]
    assert "sendChatAction" in url
    body = adapter._client.post.call_args.kwargs["json"]
    assert body == {"chat_id": 123, "action": "typing"}


@pytest.mark.asyncio
async def test_delete_message_swallows_errors(adapter):
    """delete_message should never raise — bot may lack permissions."""
    adapter._client.post = AsyncMock(side_effect=Exception("forbidden"))
    await adapter.delete_message(123, 456)  # no exception
