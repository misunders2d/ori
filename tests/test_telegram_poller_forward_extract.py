"""Proposal §7.6 — forward-extract poller short-circuit tests.

Drives the module-level `_handle_forward_extract` helper directly so
the full long-poll loop isn't required. Covers:
    - channel forward → captures chat_id, title, username.
    - group / supergroup forward (via forward_origin.type == 'chat').
    - DM-from-user forward → explains DM aliases not supported.
    - hidden_user forward → explains restricted attribution.
    - legacy `forward_from_chat`-only payload.
    - legacy `forward_from` (DM) → same DM-not-supported reply.
    - non-DM chat_type → not consumed.
    - no forward metadata → not consumed.
    - 5-min TTL on last_forward_cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import telegram_store
from interfaces import telegram_poller


class _FakeAdapter:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    telegram_store._reset_for_tests()
    yield tmp_path


@pytest.fixture
def adapter():
    return _FakeAdapter()


# --------------------------------------------------------------------------- channel / supergroup / group


@pytest.mark.asyncio
async def test_channel_forward_captures_chat_id_and_stashes(
    isolated_store, adapter
):
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {
                "id": -1001234567890,
                "title": "Eng Team",
                "username": "eng_chat",
                "type": "channel",
            },
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    assert len(adapter.messages) == 1
    reply = adapter.messages[0][1]
    assert "channel" in reply
    assert "tg_-1001234567890" in reply
    assert "Eng Team" in reply
    assert "@eng_chat" in reply
    # Stashed in last_forward_cache for `/alias save`.
    captured = telegram_store.peek_forward("tg_111")
    assert captured is not None
    assert captured.chat_id == -1001234567890
    assert captured.chat_type == "channel"


@pytest.mark.asyncio
async def test_supergroup_forward_via_chat_type(isolated_store, adapter):
    msg = {
        "forward_origin": {
            "type": "chat",
            "sender_chat": {
                "id": -1009999999999,
                "title": "Devs Hangout",
                "type": "supergroup",
            },
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    captured = telegram_store.peek_forward("tg_111")
    assert captured is not None
    assert captured.chat_id == -1009999999999
    assert captured.chat_type == "supergroup"


@pytest.mark.asyncio
async def test_group_forward_via_chat_type(isolated_store, adapter):
    msg = {
        "forward_origin": {
            "type": "chat",
            "sender_chat": {
                "id": -10010001,
                "title": "Friends Group",
                "type": "group",
            },
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    captured = telegram_store.peek_forward("tg_111")
    assert captured is not None
    assert captured.chat_type == "group"


# --------------------------------------------------------------------------- user / hidden_user (NOT stashable v1)


@pytest.mark.asyncio
async def test_user_forward_explains_dm_unsupported(isolated_store, adapter):
    msg = {
        "forward_origin": {
            "type": "user",
            "sender_user": {
                "id": 999,
                "first_name": "Sergey",
                "username": "sergey",
            },
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    reply = adapter.messages[0][1]
    assert "DM" in reply
    # NOT stashed.
    assert telegram_store.peek_forward("tg_111") is None


@pytest.mark.asyncio
async def test_hidden_user_forward_explains_restriction(
    isolated_store, adapter
):
    msg = {
        "forward_origin": {
            "type": "hidden_user",
            "sender_user_name": "Hidden Sergey",
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    reply = adapter.messages[0][1]
    assert "Hidden Sergey" in reply
    assert "restricted" in reply
    assert telegram_store.peek_forward("tg_111") is None


# --------------------------------------------------------------------------- legacy fields


@pytest.mark.asyncio
async def test_legacy_forward_from_chat_field(isolated_store, adapter):
    msg = {
        "forward_from_chat": {
            "id": -1005555,
            "title": "Legacy Channel",
            "username": "legacy",
            "type": "channel",
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    captured = telegram_store.peek_forward("tg_111")
    assert captured is not None
    assert captured.chat_id == -1005555


@pytest.mark.asyncio
async def test_legacy_forward_from_user_field(isolated_store, adapter):
    msg = {
        "forward_from": {
            "id": 999,
            "first_name": "Old",
            "username": "old",
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    # Treated as DM forward → not stashed.
    assert telegram_store.peek_forward("tg_111") is None


# --------------------------------------------------------------------------- non-trigger cases


@pytest.mark.asyncio
async def test_non_dm_chat_type_not_consumed(isolated_store, adapter):
    """Even a forward inside a group doesn't get stashed in v1; the
    poller hands the message back to normal processing."""
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {"id": -1001234, "title": "X", "type": "channel"},
        }
    }
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=-1009999, chat_type="supergroup",
        session_id="tg_-1009999",
    )
    assert handled is False
    assert adapter.messages == []


@pytest.mark.asyncio
async def test_no_forward_metadata_not_consumed(isolated_store, adapter):
    msg = {"text": "hello"}
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is False
    assert adapter.messages == []


@pytest.mark.asyncio
async def test_last_forward_ttl_expires(isolated_store, adapter):
    """5-minute TTL is enforced by telegram_store.peek_forward; ensure
    the stash actually decays."""
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {"id": -1001234, "title": "X", "type": "channel"},
        }
    }
    await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    entry = telegram_store._last_forward_cache["tg_111"]
    entry.captured_at = datetime.now(timezone.utc) - timedelta(seconds=600)
    assert telegram_store.peek_forward("tg_111") is None


@pytest.mark.asyncio
async def test_partial_origin_returns_handled_without_stashing(
    isolated_store, adapter
):
    """forward_origin.type == 'channel' but missing chat.id → friendly
    reply, no stash."""
    msg = {"forward_origin": {"type": "channel", "chat": {"title": "X"}}}
    handled = await telegram_poller._handle_forward_extract(
        adapter, msg, chat_id=111, chat_type="private", session_id="tg_111"
    )
    assert handled is True
    assert "partial" in adapter.messages[0][1].lower()
    assert telegram_store.peek_forward("tg_111") is None
