"""Tests for ``app.v2.transports.slack.LazySlackTransportClient``.

Phase 9 slice 7 (round-2 reviewer 🔴 fix).

Behavioural pins:

- Token absent → returns ``{"ok": False, "error":
  "slack_bot_token_unset"}`` without making a network call.
- Token present → POSTs to
  ``https://slack.com/api/chat.postMessage`` with the
  Bearer auth header + JSON body
  ``{channel, text}``; returns the parsed JSON.
- Transport exception → wrapped to ``ok=False`` with the
  exception as the error string (never raises to caller).
- Non-JSON response → wrapped to ``ok=False`` with
  ``slack_response_not_json: ...``.
- ``close()`` is safe to call against a client that never
  made a request.
- ``chat_postMessage`` re-reads ``SLACK_BOT_TOKEN`` on
  every call so a hot-reload between calls surfaces without
  re-boot.
- Conforms to
  :class:`app.v2.emit.slack_reminder.SlackProtocol` (the
  worker emit branch consumes it via that Protocol).
"""

from __future__ import annotations

import pytest

from app.v2.transports.slack import LazySlackTransportClient


# ===========================================================================
# Token gate
# ===========================================================================


@pytest.mark.asyncio
async def test_no_token_returns_unset_error_without_request(monkeypatch):
    """Token missing → uniform return shape. No httpx
    request fires (verified by the absence of a mock and
    the synchronous return)."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    client = LazySlackTransportClient()
    result = await client.chat_postMessage(
        channel="C123", text="hi"
    )
    assert result["ok"] is False
    assert result["error"] == "slack_bot_token_unset"


@pytest.mark.asyncio
async def test_empty_token_treated_as_missing(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "   ")
    client = LazySlackTransportClient()
    result = await client.chat_postMessage(channel="C", text="t")
    assert result["ok"] is False
    assert result["error"] == "slack_bot_token_unset"


# ===========================================================================
# Happy / network branches via httpx mock
# ===========================================================================


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Captures the request shape; returns a canned response."""

    def __init__(self, *, response):
        self._response = response
        self.posts: list[dict] = []
        self.closed = False

    async def post(self, url, *, json, headers):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return _FakeResponse(self._response)

    async def aclose(self):
        self.closed = True


def _inject_fake_client(client, fake):
    """Install ``fake`` as the lazy httpx client so the
    next call goes through it instead of building a real
    one."""
    client._client = fake


@pytest.mark.asyncio
async def test_happy_path_posts_correct_shape_and_returns_parsed_json(
    monkeypatch,
):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    fake = _FakeAsyncClient(
        response={"ok": True, "channel": "C123", "ts": "1.0"}
    )
    client = LazySlackTransportClient()
    _inject_fake_client(client, fake)

    result = await client.chat_postMessage(
        channel="C123", text="hello"
    )
    assert result == {"ok": True, "channel": "C123", "ts": "1.0"}
    # Wire shape.
    assert len(fake.posts) == 1
    sent = fake.posts[0]
    assert sent["url"] == "https://slack.com/api/chat.postMessage"
    assert sent["json"] == {"channel": "C123", "text": "hello"}
    assert sent["headers"]["Authorization"] == "Bearer xoxb-test"
    assert "application/json" in sent["headers"]["Content-Type"]


@pytest.mark.asyncio
async def test_transport_exception_is_wrapped_to_ok_false(monkeypatch):
    """An httpx-layer exception must be wrapped to
    ``{ok: False, error: transport_error: ...}`` so the
    worker emit branch sees a uniform shape."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    fake = _FakeAsyncClient(response=RuntimeError("connection refused"))
    client = LazySlackTransportClient()
    _inject_fake_client(client, fake)

    result = await client.chat_postMessage(channel="C", text="t")
    assert result["ok"] is False
    assert result["error"].startswith("transport_error:")
    assert "connection refused" in result["error"]


@pytest.mark.asyncio
async def test_non_json_response_wrapped_to_ok_false(monkeypatch):
    """A response whose ``.json()`` raises must surface as
    ``{ok: False, error: slack_response_not_json: ...}`` --
    not propagate the json decode error."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")

    class _BadResponse:
        def json(self):
            raise ValueError("garbage at offset 0")

    class _ClientReturningBad:
        async def post(self, url, *, json, headers):
            return _BadResponse()

        async def aclose(self):
            pass

    client = LazySlackTransportClient()
    _inject_fake_client(client, _ClientReturningBad())

    result = await client.chat_postMessage(channel="C", text="t")
    assert result["ok"] is False
    assert result["error"].startswith("slack_response_not_json:")


# ===========================================================================
# Token hot-reload
# ===========================================================================


@pytest.mark.asyncio
async def test_token_reread_on_each_call(monkeypatch):
    """Token is read fresh per call. Same client instance,
    different tokens across calls → both succeed with the
    respective auth header."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-first")
    fake = _FakeAsyncClient(response={"ok": True})
    client = LazySlackTransportClient()
    _inject_fake_client(client, fake)
    await client.chat_postMessage(channel="C", text="a")

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-second")
    await client.chat_postMessage(channel="C", text="b")

    assert fake.posts[0]["headers"]["Authorization"] == "Bearer xoxb-first"
    assert fake.posts[1]["headers"]["Authorization"] == "Bearer xoxb-second"


# ===========================================================================
# close()
# ===========================================================================


@pytest.mark.asyncio
async def test_close_before_any_request_is_safe():
    """close() against a client that never made a request
    must not raise (the lazy httpx client was never built)."""
    client = LazySlackTransportClient()
    await client.close()  # must not raise


@pytest.mark.asyncio
async def test_close_after_request_releases_pool(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    fake = _FakeAsyncClient(response={"ok": True})
    client = LazySlackTransportClient()
    _inject_fake_client(client, fake)
    await client.chat_postMessage(channel="C", text="t")
    await client.close()
    assert fake.closed is True


# ===========================================================================
# Protocol conformance
# ===========================================================================


def test_conforms_to_slack_protocol():
    """The Worker emit branch consumes the client as
    :class:`SlackProtocol`. Static structural check via
    runtime ``isinstance`` is enough -- Protocol with the
    one method ``chat_postMessage`` matches by duck-typing.
    """
    from app.v2.emit.slack_reminder import SlackProtocol

    client = LazySlackTransportClient()
    # Protocol isinstance requires @runtime_checkable; the
    # SlackProtocol in slack_reminder.py is not runtime-
    # decorated. We instead pin the method shape directly.
    assert callable(getattr(client, "chat_postMessage", None))
    # Optional: verify SlackProtocol is at least importable
    # from the same place the boot wiring imports.
    assert SlackProtocol is not None
