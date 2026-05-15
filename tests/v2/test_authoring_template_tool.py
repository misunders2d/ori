"""Tests for ``app.v2.authoring.templates``.

Phase 9 slice 3 per ``docs/PHASE_9_PLAN.md`` §5.2.

Pins:
- DI-leak (round-2 reviewer L500): the closure returned by
  ``make_schedule_create_reminder`` exposes EXACTLY
  ``(at, recipient_channel, text)`` — no DI parameter
  name leaks into ``inspect.signature``. The
  ``FunctionTool`` wrapping the closure carries the same
  signature.
- Happy path: closure runs the full
  build → draft → dry_run → freeze → commit pipeline and
  returns ``ok(schedule_id, spec)``; DB row +
  schedule_created event present; draft + handshake
  cleaned up.
- Naive ISO `at` → `validation_failed(naive_at_datetime)`.
- Invalid ISO `at` → `validation_failed(invalid_iso_datetime)`.
- Cache absent + no slack_client → `cache_unavailable`.
- Cache absent + refresh raises → `cache_unavailable`
  with the error message.
- Cache present + channel not found → `not_found`.
- Cache present + channel ambiguous →
  `validation_failed(channel_ambiguous)`.
- Empty / oversize text → `validation_failed(
  one_off_reminder_args_invalid)`.
- ToolDescriptor entry present + tags correct.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.adk.tools.function_tool import FunctionTool

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.templates import (
    SCHEDULE_CREATE_REMINDER_TOOL_NAME,
    make_schedule_create_reminder,
)
from app.v2.enums import EventKind
from app.v2.migrations import runner
from app.v2.models.common import UserRef
from app.v2.registry_cache.schemas import (
    SlackChannelEntry,
    SlackChannelsCache,
)
from app.v2.storage.events import list_events_for_schedule
from app.v2.storage.schedules import get_schedule
from app.v2.tool_tags import ToolCapabilityTag
from app.v2.toolsets.authoring import AUTHORING_TOOL_DESCRIPTORS


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_FIRE_AT_ISO = (_UTC_NOW + timedelta(hours=1)).isoformat()


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _event_id_factory() -> str:
    return "11111111-1111-1111-1111-111111111111"


def _schedule_id_factory() -> str:
    return "sched_reminder_alpha"


def _owner() -> UserRef:
    return UserRef(
        platform="slack",
        user_id="U_OWNER",
        display_name="Sergey",
    )


def _populated_cache() -> SlackChannelsCache:
    return SlackChannelsCache(
        workspace_id="T_TEST",
        fetched_at=_UTC_NOW - timedelta(minutes=5),
        source="stub",
        channels=[
            SlackChannelEntry(
                id="C012ABCDE",
                name="general",
                is_archived=False,
            ),
            SlackChannelEntry(
                id="C999DUPLE",
                name="dupe",
                is_archived=False,
            ),
            SlackChannelEntry(
                id="C888DUPLE",
                name="dupe",
                is_archived=False,
            ),
        ],
    )


def _migrate_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _build_closure(
    tmp_path: Path,
    *,
    cache: SlackChannelsCache | None = None,
    slack_client=None,
):
    draft_base = tmp_path / "drafts"
    handshake_base = tmp_path / "handshakes"
    drafts = DraftStore(base=draft_base)
    handshakes = HandshakeStore(base=handshake_base)
    db_path = tmp_path / "scheduler.db"
    _migrate_conn(tmp_path).close()  # init schema once

    saved_caches: list[SlackChannelsCache] = []

    def cache_loader() -> SlackChannelsCache | None:
        return cache

    def cache_saver(fresh: SlackChannelsCache) -> None:
        saved_caches.append(fresh)

    def conn_factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    closure = make_schedule_create_reminder(
        store=drafts,
        handshake_store=handshakes,
        conn_factory=conn_factory,
        cache_loader=cache_loader,
        cache_saver=cache_saver,
        slack_client=slack_client,
        expected_owner_id="T_TEST",
        clock=_fixed_clock,
        event_id_factory=_event_id_factory,
        schedule_id_factory=_schedule_id_factory,
        owner=_owner(),
        session_id="sess1",
    )
    return closure, drafts, handshakes, conn_factory, saved_caches


# ===========================================================================
# DI-leak pin (round-2 reviewer L500)
# ===========================================================================


def test_closure_signature_exposes_only_llm_params(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    sig = inspect.signature(closure)
    param_names = list(sig.parameters.keys())
    assert param_names == ["at", "recipient_channel", "text"]


def test_function_tool_signature_exposes_only_llm_params(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    tool = FunctionTool(func=closure)
    sig = inspect.signature(tool.func)
    param_names = list(sig.parameters.keys())
    assert param_names == ["at", "recipient_channel", "text"]


def test_closure_name_matches_constant(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    assert closure.__name__ == SCHEDULE_CREATE_REMINDER_TOOL_NAME


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_creates_schedule(tmp_path):
    closure, drafts, handshakes, conn_factory, _ = _build_closure(
        tmp_path, cache=_populated_cache()
    )

    response = await closure(
        _FIRE_AT_ISO, "general", "ping the team"
    )

    assert response.status == "ok", f"unexpected: {response!r}"
    assert response.schedule_id == "sched_reminder_alpha"
    assert response.spec is not None
    assert response.spec["id"] == "sched_reminder_alpha"
    assert response.spec["template"]["name"] == "OneOffReminder"
    assert response.spec["template"]["args"] == {"text": "ping the team"}

    # DB row + event present.
    conn = conn_factory()
    try:
        row = get_schedule(conn, "sched_reminder_alpha")
        assert row is not None
        events = list_events_for_schedule(conn, "sched_reminder_alpha")
        assert any(
            e.kind is EventKind.SCHEDULE_CREATED for e in events
        )
    finally:
        conn.close()

    # Cleanup: draft + handshake removed.
    with pytest.raises(FileNotFoundError):
        drafts.read("sess1", "sched_reminder_alpha")
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_reminder_alpha")


@pytest.mark.asyncio
async def test_happy_path_delivery_target_is_channel_external_id(tmp_path):
    """Pin: the resolved channel's id lands as
    ``delivery.target_session_id``."""
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(_FIRE_AT_ISO, "general", "hi")
    assert response.status == "ok"
    assert response.spec["delivery"]["target_session_id"] == "C012ABCDE"


# ===========================================================================
# `at` parsing failures
# ===========================================================================


@pytest.mark.asyncio
async def test_naive_at_returns_validation_failed(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(
        "2026-05-15T16:00:00", "general", "hi"
    )
    assert response.status == "validation_failed"
    assert any(i.code == "naive_at_datetime" for i in response.issues)


@pytest.mark.asyncio
async def test_invalid_iso_at_returns_validation_failed(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure("not-a-date", "general", "hi")
    assert response.status == "validation_failed"
    assert any(i.code == "invalid_iso_datetime" for i in response.issues)


# ===========================================================================
# Channel resolution failures
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_absent_no_client_returns_cache_unavailable(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=None, slack_client=None
    )
    response = await closure(_FIRE_AT_ISO, "general", "hi")
    assert response.status == "cache_unavailable"
    assert response.cache_kind == "slack_channels"
    assert "no slack_client configured" in response.network_error


@pytest.mark.asyncio
async def test_cache_absent_refresh_raises_returns_cache_unavailable(
    tmp_path,
):
    class FailingClient:
        def list_conversations(self):
            raise RuntimeError("simulated slack outage")

    closure, *_ = _build_closure(
        tmp_path, cache=None, slack_client=FailingClient()
    )
    response = await closure(_FIRE_AT_ISO, "general", "hi")
    assert response.status == "cache_unavailable"
    assert "simulated slack outage" in response.network_error


@pytest.mark.asyncio
async def test_channel_not_in_cache_returns_not_found(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(_FIRE_AT_ISO, "missing-channel", "hi")
    assert response.status == "not_found"
    assert "missing-channel" in response.message


@pytest.mark.asyncio
async def test_channel_ambiguous_returns_validation_failed(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(_FIRE_AT_ISO, "dupe", "hi")
    assert response.status == "validation_failed"
    assert any(
        i.code == "channel_ambiguous" for i in response.issues
    )


# ===========================================================================
# Template-args failures
# ===========================================================================


@pytest.mark.asyncio
async def test_empty_text_returns_validation_failed(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(_FIRE_AT_ISO, "general", "")
    assert response.status == "validation_failed"
    assert any(
        i.code == "one_off_reminder_args_invalid"
        for i in response.issues
    )


@pytest.mark.asyncio
async def test_oversize_text_returns_validation_failed(tmp_path):
    closure, *_ = _build_closure(
        tmp_path, cache=_populated_cache()
    )
    response = await closure(
        _FIRE_AT_ISO, "general", "x" * 4001
    )
    assert response.status == "validation_failed"
    assert any(
        i.code == "one_off_reminder_args_invalid"
        for i in response.issues
    )


# ===========================================================================
# ToolDescriptor registration
# ===========================================================================


def test_descriptor_present_with_expected_tags():
    descriptor = next(
        d
        for d in AUTHORING_TOOL_DESCRIPTORS
        if d.name == SCHEDULE_CREATE_REMINDER_TOOL_NAME
    )
    assert descriptor.tags == {
        ToolCapabilityTag.DB_WRITE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.USES_OAUTH,
    }
