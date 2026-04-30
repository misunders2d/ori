"""Unit tests for app.tools.telegram.telegram_send_dm."""

from __future__ import annotations

import pytest

from app.runtime import roster, transport
from app.tools.telegram import telegram_send_dm


class _FakeTelegramAdapter:
    """Minimal stand-in for TelegramAdapter — captures send_message calls."""

    platform_name = "telegram"

    def __init__(self):
        self.sent: list[tuple] = []
        self.fail_with: Exception | None = None

    async def send_message(self, chat_id, text):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((chat_id, text))


@pytest.fixture
def isolated_roster(tmp_path, monkeypatch):
    p = tmp_path / "roster.json"
    monkeypatch.setattr(roster, "ROSTER_PATH", str(p))
    return p


@pytest.fixture
def fake_adapter():
    """Register a fake adapter, restore registry after test."""
    adapter = _FakeTelegramAdapter()
    original_registry = dict(transport._registry)
    transport.register_adapter(adapter)
    yield adapter
    transport._registry.clear()
    transport._registry.update(original_registry)


@pytest.fixture
def no_adapter():
    """Empty adapter registry."""
    original_registry = dict(transport._registry)
    transport._registry.clear()
    yield
    transport._registry.clear()
    transport._registry.update(original_registry)


@pytest.mark.asyncio
async def test_success(isolated_roster, fake_adapter):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    result = await telegram_send_dm(person="Ruslan", text="hello")
    assert result["status"] == "success"
    assert result["chat_id"] == 111
    assert fake_adapter.sent == [(111, "hello")]


@pytest.mark.asyncio
async def test_not_found_when_roster_empty(isolated_roster, fake_adapter):
    result = await telegram_send_dm(person="Ruslan", text="hello")
    assert result["status"] == "not_found"
    assert "first messaged the bot" in result["message"]
    assert fake_adapter.sent == []


@pytest.mark.asyncio
async def test_not_found_when_no_match(isolated_roster, fake_adapter):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    result = await telegram_send_dm(person="Hans", text="hi")
    assert result["status"] == "not_found"
    assert fake_adapter.sent == []


@pytest.mark.asyncio
async def test_ambiguous_returns_candidates(isolated_roster, fake_adapter):
    roster.record_user("tg_a", "telegram", 1, first_name="Anna")
    roster.record_user("tg_b", "telegram", 2, first_name="Anastasia")
    result = await telegram_send_dm(person="An", text="ping")
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2
    user_ids = {c["user_id"] for c in result["candidates"]}
    assert user_ids == {"tg_a", "tg_b"}
    assert fake_adapter.sent == []


@pytest.mark.asyncio
async def test_error_when_adapter_not_registered(isolated_roster, no_adapter):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    result = await telegram_send_dm(person="Ruslan", text="hello")
    assert result["status"] == "error"
    assert "not registered" in result["message"]


@pytest.mark.asyncio
async def test_error_propagates_send_failure(isolated_roster, fake_adapter):
    fake_adapter.fail_with = RuntimeError("network down")
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    result = await telegram_send_dm(person="Ruslan", text="hello")
    assert result["status"] == "error"
    assert "network down" in result["message"]


@pytest.mark.asyncio
async def test_at_handle_resolves(isolated_roster, fake_adapter):
    roster.record_user("tg_111", "telegram", 111, username="rshostak")
    result = await telegram_send_dm(person="@rshostak", text="hi")
    assert result["status"] == "success"
    assert fake_adapter.sent == [(111, "hi")]


@pytest.mark.asyncio
async def test_only_telegram_platform_matched(isolated_roster, fake_adapter):
    """A slack roster entry with the same name shouldn't match a telegram DM."""
    roster.record_user("sl_x", "slack", "U999", first_name="Ruslan")
    result = await telegram_send_dm(person="Ruslan", text="hi")
    assert result["status"] == "not_found"
