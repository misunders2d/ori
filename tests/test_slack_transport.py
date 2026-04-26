"""SlackAdapter — id helpers, secret scrubbing, send_message, send_media routing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.transports.slack.adapter import SlackAdapter, _scrub_secrets


@pytest.fixture
def adapter():
    return SlackAdapter(client=MagicMock(), bot_token="xoxb-test-token")


def test_platform_name(adapter):
    assert adapter.platform_name == "slack"


def test_make_session_id(adapter):
    assert adapter.make_session_id("C012345") == "sl_C012345"


def test_make_user_id(adapter):
    assert adapter.make_user_id("U987") == "sl_U987"


def test_parse_notify_info(adapter):
    assert adapter.parse_notify_info("sl_C012345") == {"type": "slack", "chat_id": "C012345"}
    assert adapter.parse_notify_info("tg_42") == {}


def test_scrub_secrets_redacts_xoxb(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-actual-bot-token-1234")
    text = "Token leaked: xoxb-actual-bot-token-1234"
    out = _scrub_secrets(text)
    assert "xoxb-actual-bot-token-1234" not in out
    assert "[REDACTED]" in out


def test_scrub_secrets_catches_xoxb_pattern():
    text = "xoxb-anything-here-not-set-in-env"
    out = _scrub_secrets(text)
    assert "[REDACTED]" in out


def test_scrub_secrets_catches_xapp_pattern():
    text = "Connect with xapp-1-A12345-67890-token"
    out = _scrub_secrets(text)
    assert "[REDACTED]" in out


@pytest.mark.asyncio
async def test_send_message_basic(adapter):
    resp = MagicMock()
    resp.json = MagicMock(return_value={"ok": True})
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_message("C012", "hello world")
    args, kwargs = adapter._client.post.call_args
    assert "chat.postMessage" in args[0]
    assert kwargs["json"]["channel"] == "C012"
    assert kwargs["json"]["text"] == "hello world"


@pytest.mark.asyncio
async def test_send_message_in_thread(adapter):
    resp = MagicMock()
    resp.json = MagicMock(return_value={"ok": True})
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.send_message("C012", "reply", thread_ts="1234.5678")
    body = adapter._client.post.call_args.kwargs["json"]
    assert body["thread_ts"] == "1234.5678"


@pytest.mark.asyncio
async def test_send_typing_is_noop(adapter):
    """Slack Web API has no REST typing indicator for bots — must not crash."""
    adapter._client.post = AsyncMock()
    result = await adapter.send_typing("C012")
    assert result is None
    # Should make zero HTTP calls
    adapter._client.post.assert_not_called()


@pytest.mark.asyncio
async def test_delete_message(adapter):
    resp = MagicMock()
    resp.json = MagicMock(return_value={"ok": True})
    adapter._client.post = AsyncMock(return_value=resp)
    await adapter.delete_message("C012", "1234.5678")
    args, kwargs = adapter._client.post.call_args
    assert "chat.delete" in args[0]
    assert kwargs["json"] == {"channel": "C012", "ts": "1234.5678"}


@pytest.mark.asyncio
async def test_delete_message_swallows_errors(adapter):
    """delete_message must not raise — bot may lack permissions."""
    adapter._client.post = AsyncMock(side_effect=Exception("forbidden"))
    await adapter.delete_message("C012", "1234.5678")  # no exception


@pytest.mark.asyncio
async def test_download_file_rejects_non_slack_url(adapter):
    """SSRF guard — only files.slack.com / *.slack.com URLs allowed."""
    result = await adapter.download_file("https://evil.example.com/file")
    assert result is None
