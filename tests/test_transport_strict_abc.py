"""Unit tests for the TransportAdapter strict-variant ABC (slice 3).

Confirms:
    - send_text_strict / send_media_strict / copy_message_strict are
      declared abstract on the base class.
    - A concrete subclass that omits any of them cannot be instantiated.
    - SlackAdapter (v1) stubs raise NotImplementedError.
    - Both existing adapter subclasses (Slack, Telegram) remain
      instantiable.

TelegramAdapter's real strict-variant behavior is exercised in
tests/test_telegram_adapter_strict.py (slice 4).
"""

from __future__ import annotations

import httpx
import pytest

from app.core import transport, transport_slack
from interfaces import telegram_poller


def test_abc_declares_strict_methods_as_abstract():
    abstracts = transport.TransportAdapter.__abstractmethods__
    assert "send_text_strict" in abstracts
    assert "send_media_strict" in abstracts
    assert "copy_message_strict" in abstracts


def test_incomplete_subclass_cannot_instantiate():
    """An adapter subclass that does not implement the new abstracts must
    fail at construction time so we cannot ship a half-wired transport."""

    class HalfBaked(transport.TransportAdapter):
        @property
        def platform_name(self) -> str:
            return "halfbaked"

        def make_session_id(self, raw_id):
            return f"hb_{raw_id}"

        def make_user_id(self, raw_id):
            return f"hb_{raw_id}"

        def parse_notify_info(self, session_id):
            return {}

        async def send_message(self, target_id, text):
            return None

        async def send_typing(self, target_id):
            return None

        async def delete_message(self, target_id, message_id):
            return None

        async def send_media(self, target_id, data, mime_type, caption=""):
            return None

        async def download_file(self, file_id):
            return None

        # NOTE: send_text_strict / send_media_strict / copy_message_strict
        # intentionally NOT implemented.

    with pytest.raises(TypeError, match="abstract"):
        HalfBaked()


@pytest.mark.asyncio
async def test_slack_adapter_stubs_raise_not_implemented():
    async with httpx.AsyncClient() as client:
        adapter = transport_slack.SlackAdapter(client, "fake-token")

    with pytest.raises(NotImplementedError, match="send_text_strict"):
        await adapter.send_text_strict("C123", "hi")
    with pytest.raises(NotImplementedError, match="send_media_strict"):
        await adapter.send_media_strict("C123", b"x", "image/png")
    with pytest.raises(NotImplementedError, match="copy_message_strict"):
        await adapter.copy_message_strict("C123", 0, 0)


def test_existing_adapter_subclasses_still_instantiable():
    """SlackAdapter and TelegramAdapter must still construct after the
    ABC widens, so the live poller is not bricked by slice 3."""
    import asyncio

    async def _build():
        async with httpx.AsyncClient() as client:
            slack = transport_slack.SlackAdapter(client, "fake-token")
            telegram = telegram_poller.TelegramAdapter(client, "fake-token")
            return slack.platform_name, telegram.platform_name

    names = asyncio.run(_build())
    assert names == ("slack", "telegram")
