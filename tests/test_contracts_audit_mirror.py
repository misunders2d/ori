"""Tests for ``app/contracts/audit_mirror.py``.

Pins:
  * ``resolve_emit_target_session`` for every adapter we ship,
    including the ``#name`` Slack fallback (returns None — only raw
    channel ids resolve cleanly without a Slack API round-trip).
  * ``mirror_emit_to_session`` no-ops cleanly when (a) the adapter
    doesn't target a chat, (b) the runner isn't ready, (c) the
    session doesn't exist yet, (d) ADK raises during append.
  * Happy path: a model-role event is appended to the resolved
    session via ``runner.session_service.append_event``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def test_resolve_slack_post_raw_channel_id():
    from app.contracts.audit_mirror import resolve_emit_target_session

    assert (
        resolve_emit_target_session("slack_post", {"channel": "C0B2LJRS8D8"})
        == "sl_C0B2LJRS8D8"
    )


def test_resolve_slack_post_with_hash_name_returns_none():
    """``#channel-name`` requires a Slack API lookup to resolve to a
    channel id. Skip the mirror — the bot's interactive ingest path
    will pick up follow-up messages from that channel as usual."""
    from app.contracts.audit_mirror import resolve_emit_target_session

    assert resolve_emit_target_session("slack_post", {"channel": "#general"}) is None


def test_resolve_slack_post_empty_channel_returns_none():
    from app.contracts.audit_mirror import resolve_emit_target_session

    assert resolve_emit_target_session("slack_post", {}) is None
    assert resolve_emit_target_session("slack_post", {"channel": ""}) is None


def test_resolve_telegram_dm_chat_id():
    from app.contracts.audit_mirror import resolve_emit_target_session

    assert (
        resolve_emit_target_session("telegram_dm", {"user_id": "330959414"})
        == "tg_330959414"
    )


def test_resolve_telegram_dm_via_chat_id_alias():
    from app.contracts.audit_mirror import resolve_emit_target_session

    assert (
        resolve_emit_target_session("telegram_dm", {"chat_id": "330959414"})
        == "tg_330959414"
    )


def test_resolve_unknown_adapter_returns_none():
    from app.contracts.audit_mirror import resolve_emit_target_session

    # Persistent-store adapters and gates have no chat session to
    # mirror into — every one of them must return None.
    assert resolve_emit_target_session("sheet_append", {"spreadsheet_id": "x"}) is None
    assert resolve_emit_target_session("drive_doc_fill", {"doc_id": "x"}) is None
    assert resolve_emit_target_session("memory_update", {"namespace": "x"}) is None
    assert resolve_emit_target_session("nonexistent_adapter", {}) is None


# ---------------------------------------------------------------------------
# Mirror dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_runner(monkeypatch):
    """Install a fake runner singleton that exposes a recording
    session_service. Tests inspect ``runner.session_service.append_event``
    after calling ``mirror_emit_to_session``."""

    class _Session:
        def __init__(self):
            self.events: list[Any] = []
            self.state = {}

    class _Service:
        def __init__(self):
            self._session: _Session | None = None
            self.append_calls: list[tuple] = []

        async def get_session(self, app_name, user_id, session_id):
            return self._session

        async def create_session(self, app_name, user_id, session_id, state=None):
            self._session = _Session()
            return self._session

        async def append_event(self, session, event):
            self.append_calls.append((session, event))

    runner = MagicMock()
    runner.app_name = "test_app"
    runner.session_service = _Service()
    monkeypatch.setattr("run_bot.get_runner", lambda: runner)
    return runner


@pytest.mark.asyncio
async def test_mirror_appends_event_to_existing_session(fake_runner):
    """Happy path: session exists, runner ready → event is appended
    with role=model and author='contract_runner'."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    # Pre-create the channel session as if a human had already
    # chatted there.
    await fake_runner.session_service.create_session(
        app_name="test_app",
        user_id="sl_C0B2LJRS8D8",
        session_id="sl_C0B2LJRS8D8",
    )

    ok = await mirror_emit_to_session(
        "slack_post",
        {"channel": "C0B2LJRS8D8", "content": "Daily AI Pilot — Auto-Pilot Growth"},
        "Daily AI Pilot — Auto-Pilot Growth",
        contract_id="ai_pilot_wed_v3",
    )

    assert ok is True
    assert len(fake_runner.session_service.append_calls) == 1
    _, event = fake_runner.session_service.append_calls[0]
    assert event.author == "contract_runner"
    assert event.content.role == "model"
    rendered = event.content.parts[0].text
    assert "Daily AI Pilot" in rendered


@pytest.mark.asyncio
async def test_mirror_noop_when_session_does_not_exist(fake_runner):
    """No human has chatted in the channel yet → session is None →
    mirror is a no-op. The poller path will hydrate the session the
    first time a human asks a follow-up."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    # fake_runner's _Service.get_session returns None until something
    # creates the session. Don't create one.
    ok = await mirror_emit_to_session(
        "slack_post",
        {"channel": "C0B2LJRS8D8", "content": "Hello"},
        "Hello",
    )
    assert ok is False
    assert fake_runner.session_service.append_calls == []


@pytest.mark.asyncio
async def test_mirror_noop_when_no_runner(monkeypatch):
    """Worker may fire in unit tests or in a degraded boot before the
    runner singleton is ready. Mirror must silently no-op rather than
    propagating the import / None state."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    monkeypatch.setattr("run_bot.get_runner", lambda: None)
    ok = await mirror_emit_to_session(
        "slack_post", {"channel": "C012", "content": "hi"}, "hi"
    )
    assert ok is False


@pytest.mark.asyncio
async def test_mirror_noop_for_unresolvable_target(fake_runner):
    """Adapters whose output isn't a chat (memory_update, sheet_append,
    drive_doc_fill) → resolve returns None → mirror no-ops without
    even touching the runner."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    ok = await mirror_emit_to_session(
        "sheet_append",
        {"spreadsheet_id": "10cSeLai", "row": ["2026-05-13", "post"]},
        "log row",
    )
    assert ok is False
    assert fake_runner.session_service.append_calls == []


@pytest.mark.asyncio
async def test_mirror_does_not_raise_on_append_failure(fake_runner, caplog):
    """ADK raise inside ``append_event`` must not propagate. Mirror is
    advisory — contract fire already succeeded by the time we get
    here, and a mirror miss must never cascade into a fire failure."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    # Pre-create the session, then sabotage append_event.
    await fake_runner.session_service.create_session(
        app_name="test_app",
        user_id="sl_C0B2LJRS8D8",
        session_id="sl_C0B2LJRS8D8",
    )

    async def _boom(session, event):
        raise RuntimeError("ADK blew up")

    fake_runner.session_service.append_event = _boom

    with caplog.at_level("WARNING"):
        ok = await mirror_emit_to_session(
            "slack_post",
            {"channel": "C0B2LJRS8D8", "content": "hi"},
            "hi",
        )

    assert ok is False
    assert any("audit_mirror" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_mirror_telegram_dm_resolves_to_tg_prefix(fake_runner):
    """``telegram_dm`` with a numeric ``user_id`` resolves to a
    ``tg_<n>`` session, matching the convention the Telegram poller
    uses (``make_session_id``)."""
    from app.contracts.audit_mirror import mirror_emit_to_session

    await fake_runner.session_service.create_session(
        app_name="test_app",
        user_id="tg_330959414",
        session_id="tg_330959414",
    )

    ok = await mirror_emit_to_session(
        "telegram_dm",
        {"user_id": "330959414", "text": "report ready"},
        "report ready",
    )

    assert ok is True
    assert len(fake_runner.session_service.append_calls) == 1
