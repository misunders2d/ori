"""Tests for ``app.v2.authoring.delivery``.

Phase 7 slice 3 per ``docs/PHASE_7_PLAN.md`` §5.4.

Pins:
- Cache present + lookup hits → ToolResponse.ok; draft's
  delivery.target_session_id matches the resolved id.
- CacheMiss → validation_failed with channel_not_in_cache.
- ChannelAmbiguous → validation_failed with channel_ambiguous
  + candidate_ids in message.
- Cache absent + slack_client=None → cache_unavailable with
  network_error "no slack_client configured".
- Cache absent + slack_client + refresh succeeds → fresh
  cache saved via cache_saver; resolution proceeds; ok.
- Cache absent + slack_client.list_conversations raises →
  cache_unavailable with network_error=str(exc).
- AST + sys.modules pins: delivery module does NOT import
  slack_sdk at module load.
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import datetime, timezone
from typing import Callable, Optional
from unittest.mock import MagicMock

import pytest

from app.v2.authoring.delivery import schedule_set_delivery
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
)
from app.v2.models.common import (
    AuditPolicy,
    FailurePolicy,
    UserRef,
)
from app.v2.models.triggers import OneOffTrigger
from app.v2.registry_cache.schemas import (
    SlackChannelEntry,
    SlackChannelsCache,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _seed_partial(tmp_path) -> tuple[DraftStore, str]:
    store = DraftStore(base=tmp_path)
    draft = ScheduleSpecDraft(
        id="sched_alpha",
        description="weekly amazon summary digest",
    )
    store.write("sess1", draft)
    return store, "sched_alpha"


def _seed_complete_minus_delivery(tmp_path) -> tuple[DraftStore, str]:
    """Every required field set EXCEPT delivery so a successful
    set_delivery call lands the draft as ``ok``."""
    store = DraftStore(base=tmp_path)
    draft = ScheduleSpecDraft(
        id="sched_alpha",
        description="weekly amazon summary digest",
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        trigger=OneOffTrigger(
            at_iso_datetime=_UTC_NOW.replace(hour=13),
            timezone="UTC",
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
    )
    store.write("sess1", draft)
    return store, "sched_alpha"


def _slack_cache(channels) -> SlackChannelsCache:
    return SlackChannelsCache(
        workspace_id="T_TEST",
        fetched_at=_UTC_NOW,
        source="slack.api.conversations.list",
        channels=channels,
    )


def _stub_loader(cache):
    return lambda: cache


class _SavingSaver:
    def __init__(self) -> None:
        self.saved: list[SlackChannelsCache] = []

    def __call__(self, cache: SlackChannelsCache) -> None:
        self.saved.append(cache)


class _RaisingSaver:
    def __call__(self, cache):  # pragma: no cover - never expected
        raise AssertionError("saver should not be called")


class _StubSlackClient:
    def __init__(self, rows, *, exc=None):
        self._rows = rows
        self._exc = exc
        self.call_count = 0

    def list_conversations(self):
        self.call_count += 1
        if self._exc is not None:
            raise self._exc
        return list(self._rows)


# ===========================================================================
# Cache present — happy paths
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_by_id_sets_delivery(tmp_path):
    store, draft_id = _seed_partial(tmp_path)
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C012", name="general"),
            SlackChannelEntry(id="C034", name="alerts"),
        ]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="C012",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "not_ready"  # partial draft
    loaded = store.read("sess1", draft_id)
    assert loaded.delivery.target_session_id == "C012"
    assert loaded.delivery.fallback_policy == (
        DeliveryFallbackPolicy.SESSION_TO_ORIGIN
    )


@pytest.mark.asyncio
async def test_resolve_by_name_with_hash_strip(tmp_path):
    store, draft_id = _seed_partial(tmp_path)
    cache = _slack_cache(
        [SlackChannelEntry(id="C012", name="general")]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="#general",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "not_ready"
    assert store.read("sess1", draft_id).delivery.target_session_id == (
        "C012"
    )


@pytest.mark.asyncio
async def test_complete_draft_returns_ok(tmp_path):
    """Set delivery on a draft missing only delivery → complete
    → validate_schedule_spec passes → ok."""
    store, draft_id = _seed_complete_minus_delivery(tmp_path)
    cache = _slack_cache(
        [SlackChannelEntry(id="C012", name="general")]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="C012",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.draft_id == draft_id


# ===========================================================================
# Cache present — error paths
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_miss_returns_validation_failed(tmp_path):
    store, draft_id = _seed_partial(tmp_path)
    cache = _slack_cache(
        [SlackChannelEntry(id="C012", name="general")]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="missing",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "validation_failed"
    assert any(i.code == "channel_not_in_cache" for i in r.issues)
    # Draft unchanged.
    assert store.read("sess1", draft_id).delivery is None


@pytest.mark.asyncio
async def test_channel_ambiguous_returns_validation_failed(tmp_path):
    store, draft_id = _seed_partial(tmp_path)
    cache = _slack_cache(
        [
            SlackChannelEntry(id="C001", name="alerts"),
            SlackChannelEntry(id="C002", name="alerts"),
        ]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="alerts",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "validation_failed"
    assert any(i.code == "channel_ambiguous" for i in r.issues)
    msg = r.issues[0].message
    assert "C001" in msg and "C002" in msg


# ===========================================================================
# Cache absent paths
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_absent_no_client_returns_cache_unavailable(
    tmp_path,
):
    store, draft_id = _seed_partial(tmp_path)
    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="general",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=lambda: None,
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "cache_unavailable"
    assert r.cache_kind == "slack_channels"
    assert "no slack_client configured" in r.network_error


@pytest.mark.asyncio
async def test_cache_absent_refresh_succeeds(tmp_path):
    """Cache absent + client present + refresh OK → cache
    saved + lookup proceeds."""
    store, draft_id = _seed_partial(tmp_path)
    client = _StubSlackClient(
        [{"id": "C012", "name": "general"}]
    )
    saver = _SavingSaver()

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="general",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=lambda: None,
        cache_saver=saver,
        slack_client=client,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "not_ready"
    assert client.call_count == 1
    # Saver received the fresh cache.
    assert len(saver.saved) == 1
    assert saver.saved[0].workspace_id == "T_TEST"
    assert saver.saved[0].channels[0].id == "C012"
    # Draft's delivery wired.
    loaded = store.read("sess1", draft_id)
    assert loaded.delivery.target_session_id == "C012"


@pytest.mark.asyncio
async def test_cache_absent_refresh_raises_returns_cache_unavailable(
    tmp_path,
):
    """Slack client raising during list_conversations →
    cache_unavailable with network_error=str(exc)."""
    store, draft_id = _seed_partial(tmp_path)
    client = _StubSlackClient(
        [], exc=RuntimeError("network down")
    )
    saver = _RaisingSaver()

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="general",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=lambda: None,
        cache_saver=saver,
        slack_client=client,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "cache_unavailable"
    assert r.cache_kind == "slack_channels"
    assert "network down" in r.network_error


# ===========================================================================
# Missing draft
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_draft_returns_not_found(tmp_path):
    store = DraftStore(base=tmp_path)
    r = await schedule_set_delivery(
        "absent",
        channel_lookup="general",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=lambda: None,
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
    )

    assert r.status == "not_found"


# ===========================================================================
# Module-level import hygiene (Protocol-typed DI, no SDK)
# ===========================================================================


def _module_imports(module) -> set[str]:
    src = inspect.getsource(module)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_delivery_module_does_not_import_slack_sdk():
    from app.v2.authoring import delivery as delivery_mod

    leaked = [
        n for n in _module_imports(delivery_mod) if "slack_sdk" in n
    ]
    assert not leaked, f"slack_sdk import leaked: {leaked!r}"


def test_delivery_module_does_not_import_googleapiclient():
    from app.v2.authoring import delivery as delivery_mod

    leaked = [
        n
        for n in _module_imports(delivery_mod)
        if "googleapiclient" in n
    ]
    assert not leaked


def test_delivery_module_does_not_bind_prod_clock():
    from app.v2.authoring import delivery as delivery_mod

    assert not hasattr(delivery_mod, "prod_clock")


# ===========================================================================
# Archive-policy passthrough
# ===========================================================================


@pytest.mark.asyncio
async def test_include_archived_resolves_archived_by_id(tmp_path):
    """The setter forwards include_archived to the resolver
    so callers can pin an archived channel by id explicitly."""
    store, draft_id = _seed_partial(tmp_path)
    cache = _slack_cache(
        [
            SlackChannelEntry(
                id="C999", name="old", is_archived=True
            )
        ]
    )

    r = await schedule_set_delivery(
        draft_id,
        channel_lookup="C999",
        fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        session_id="sess1",
        store=store,
        cache_loader=_stub_loader(cache),
        cache_saver=_RaisingSaver(),
        slack_client=None,
        clock=_fixed_clock,
        expected_owner_id="T_TEST",
        include_archived=True,
    )

    assert r.status == "not_ready"
    assert store.read("sess1", draft_id).delivery.target_session_id == (
        "C999"
    )
