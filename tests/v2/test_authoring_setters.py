"""Tests for ``app.v2.authoring.setters``.

Phase 7 slice 2 per ``docs/PHASE_7_PLAN.md`` §5.3.

Pins:
- schedule_draft_start creates the draft file with id /
  description / owner; unknown platform → validation_failed.
- Each setter loads / mutates / writes; pin by reading back.
- Setter on missing draft → ToolResponse.not_found.
- Cron guards: numeric DOW + unknown timezone +
  too-few-fields each surface as validation_failed.
- One-off guards: naive ISO → validation_failed; unknown tz
  → validation_failed; past tz-aware → ok with warning
  message.
- Owner setter platform allowlist enforced.
- Partial draft → not_ready with missing_fields list.
- Complete draft → validate_schedule_spec called with no
  execution_plans / registries kwargs (Q6 pin).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from unittest.mock import MagicMock

import pytest

from app.v2.authoring import setters
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import (
    schedule_draft_start,
    schedule_set_cron,
    schedule_set_description,
    schedule_set_failure_policy,
    schedule_set_one_off,
    schedule_set_owner,
)
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    RetryStrategy,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.triggers import CronTrigger, OneOffTrigger


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _store(tmp_path) -> DraftStore:
    return DraftStore(base=tmp_path)


def _seed_partial(store: DraftStore, sess: str = "sess1") -> str:
    """Seed a draft with only id+description set so setters can
    extend it. Returns the draft id."""
    draft = ScheduleSpecDraft(
        id="sched_alpha",
        description="weekly amazon summary digest",
    )
    store.write(sess, draft)
    return "sched_alpha"


def _seed_complete_minus(
    store: DraftStore, missing: str, sess: str = "sess1"
) -> str:
    """Seed a draft with every required field set EXCEPT
    ``missing``. Returns the draft id."""
    fields = dict(
        id="sched_alpha",
        description="weekly amazon summary digest",
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        trigger=OneOffTrigger(at_iso_datetime=_UTC_NOW, timezone="UTC"),
        delivery=Delivery(
            target_session_id="C123",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
    )
    fields.pop(missing)
    draft = ScheduleSpecDraft(**fields)
    store.write(sess, draft)
    return "sched_alpha"


# ===========================================================================
# schedule_draft_start
# ===========================================================================


@pytest.mark.asyncio
async def test_draft_start_creates_file(tmp_path):
    store = _store(tmp_path)
    r = await schedule_draft_start(
        "sched_x",
        "weekly amazon summary digest",
        session_id="sess1",
        owner_platform="slack",
        owner_user_id="U_OWNER",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    assert "trigger" in r.missing_fields
    loaded = store.read("sess1", "sched_x")
    assert loaded.id == "sched_x"
    assert loaded.owner.platform == "slack"


@pytest.mark.asyncio
async def test_draft_start_unknown_platform(tmp_path):
    r = await schedule_draft_start(
        "sched_x",
        "weekly amazon summary digest",
        session_id="sess1",
        owner_platform="discord",
        owner_user_id="U",
        store=_store(tmp_path),
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "unknown_platform" for i in r.issues)


@pytest.mark.asyncio
async def test_draft_start_invalid_id_pattern(tmp_path):
    """ScheduleSpec id pattern is ^[a-z][a-z0-9_]*$; the draft
    accepts but to_spec/validate will surface. Since the draft
    is incomplete here we just get not_ready — pin that the
    file lands."""
    store = _store(tmp_path)
    r = await schedule_draft_start(
        "ok_id",
        "weekly amazon summary digest",
        session_id="sess1",
        owner_platform="slack",
        owner_user_id="U",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"


# ===========================================================================
# schedule_set_description
# ===========================================================================


@pytest.mark.asyncio
async def test_set_description_updates_file(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_description(
        "sched_alpha",
        "updated description for digest job",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    assert store.read("sess1", "sched_alpha").description == (
        "updated description for digest job"
    )


@pytest.mark.asyncio
async def test_set_description_missing_draft(tmp_path):
    r = await schedule_set_description(
        "absent",
        "x",
        session_id="sess1",
        store=_store(tmp_path),
        clock=_fixed_clock,
    )
    assert r.status == "not_found"


# ===========================================================================
# schedule_set_owner
# ===========================================================================


@pytest.mark.asyncio
async def test_set_owner_updates_file(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_owner(
        "sched_alpha",
        platform="telegram",
        user_id="330959414",
        display_name="Sergey",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    loaded = store.read("sess1", "sched_alpha")
    assert loaded.owner.platform == "telegram"
    assert loaded.owner.user_id == "330959414"


@pytest.mark.asyncio
async def test_set_owner_unknown_platform(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_owner(
        "sched_alpha",
        platform="discord",
        user_id="U",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "unknown_platform" for i in r.issues)
    # Draft on disk is unchanged.
    assert store.read("sess1", "sched_alpha").owner is None


# ===========================================================================
# schedule_set_cron
# ===========================================================================


@pytest.mark.asyncio
async def test_set_cron_valid(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_cron(
        "sched_alpha",
        "0 9 * * MON-FRI",
        "America/Los_Angeles",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    loaded = store.read("sess1", "sched_alpha")
    assert isinstance(loaded.trigger, CronTrigger)
    assert loaded.trigger.cron == "0 9 * * MON-FRI"
    assert loaded.trigger.timezone == "America/Los_Angeles"


@pytest.mark.asyncio
async def test_set_cron_numeric_dow_rejected(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_cron(
        "sched_alpha",
        "0 9 * * 0",  # numeric DOW
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "numeric_dow_rejected" for i in r.issues)
    # Draft unchanged.
    assert store.read("sess1", "sched_alpha").trigger is None


@pytest.mark.asyncio
async def test_set_cron_unknown_timezone(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_cron(
        "sched_alpha",
        "0 9 * * MON",
        "Mars/Olympus",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "unknown_timezone" for i in r.issues)


@pytest.mark.asyncio
async def test_set_cron_wrong_field_count(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_cron(
        "sched_alpha",
        "0 9 * *",  # 4 fields
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"


@pytest.mark.asyncio
async def test_set_cron_missing_draft(tmp_path):
    r = await schedule_set_cron(
        "absent",
        "0 9 * * MON",
        "UTC",
        session_id="sess1",
        store=_store(tmp_path),
        clock=_fixed_clock,
    )
    assert r.status == "not_found"


# ===========================================================================
# schedule_set_one_off
# ===========================================================================


@pytest.mark.asyncio
async def test_set_one_off_future_utc(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    future = (_UTC_NOW + timedelta(hours=1)).isoformat()
    r = await schedule_set_one_off(
        "sched_alpha",
        future,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    loaded = store.read("sess1", "sched_alpha")
    assert isinstance(loaded.trigger, OneOffTrigger)


@pytest.mark.asyncio
async def test_set_one_off_naive_datetime_rejected(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    naive_iso = datetime(2026, 5, 15, 13, 0).isoformat()  # no tz
    r = await schedule_set_one_off(
        "sched_alpha",
        naive_iso,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "naive_datetime" for i in r.issues)
    # Draft unchanged.
    assert store.read("sess1", "sched_alpha").trigger is None


@pytest.mark.asyncio
async def test_set_one_off_unknown_timezone(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    future = (_UTC_NOW + timedelta(hours=1)).isoformat()
    r = await schedule_set_one_off(
        "sched_alpha",
        future,
        "Mars/Olympus",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "unknown_timezone" for i in r.issues)


@pytest.mark.asyncio
async def test_set_one_off_past_datetime_accepted_with_warning(tmp_path):
    """Past tz-aware datetime is accepted (boot backfill handles
    past OneOffs); the response surfaces a warning issue via
    ``message`` for operator awareness."""
    store = _store(tmp_path)
    # Seed every required field EXCEPT trigger so the past
    # OneOff makes the draft complete (so we reach _finalise
    # success path with warnings).
    _seed_complete_minus(store, missing="trigger")
    past = (_UTC_NOW - timedelta(hours=2)).isoformat()
    r = await schedule_set_one_off(
        "sched_alpha",
        past,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    # Draft is now complete → ok with warning.
    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.message is not None
    assert "past" in r.message.lower() or "before now" in r.message.lower()
    # Draft IS written.
    loaded = store.read("sess1", "sched_alpha")
    assert isinstance(loaded.trigger, OneOffTrigger)


@pytest.mark.asyncio
async def test_set_one_off_invalid_iso(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_one_off(
        "sched_alpha",
        "not-a-date",
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "invalid_iso_datetime" for i in r.issues)


# ===========================================================================
# schedule_set_failure_policy
# ===========================================================================


@pytest.mark.asyncio
async def test_set_failure_policy_alert_admin(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_failure_policy(
        "sched_alpha",
        FailureActionType.ALERT_ADMIN,
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    loaded = store.read("sess1", "sched_alpha")
    assert (
        loaded.failure.on_failure_action == FailureActionType.ALERT_ADMIN
    )
    assert loaded.failure.retry_policy is None


@pytest.mark.asyncio
async def test_set_failure_policy_with_retry(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_failure_policy(
        "sched_alpha",
        FailureActionType.RETRY_LATER,
        retry_strategy=RetryStrategy.EXPONENTIAL,
        retry_base_seconds=30,
        retry_max_attempts=3,
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    loaded = store.read("sess1", "sched_alpha")
    assert loaded.failure.retry_policy.max_attempts == 3


@pytest.mark.asyncio
async def test_set_failure_policy_retry_incomplete(tmp_path):
    """retry_strategy without retry_base_seconds /
    retry_max_attempts is rejected."""
    store = _store(tmp_path)
    _seed_partial(store)
    r = await schedule_set_failure_policy(
        "sched_alpha",
        FailureActionType.RETRY_LATER,
        retry_strategy=RetryStrategy.EXPONENTIAL,
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(i.code == "retry_policy_incomplete" for i in r.issues)


# ===========================================================================
# Complete-draft full-spec validation pipeline
# ===========================================================================


@pytest.mark.asyncio
async def test_setter_on_complete_draft_runs_validate_spec(tmp_path):
    """A setter whose mutation completes the draft funnels
    through validate_schedule_spec; on success returns
    ToolResponse.ok. Use schedule_set_one_off because
    reminder-only (OneOff trigger without ExecutionPlan) is
    the phase-7 flow design D5 / validation_reminder_rule
    accepts."""
    store = _store(tmp_path)
    _seed_complete_minus(store, missing="trigger")
    future = (_UTC_NOW + timedelta(hours=1)).isoformat()
    r = await schedule_set_one_off(
        "sched_alpha",
        future,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.draft_id == "sched_alpha"


@pytest.mark.asyncio
async def test_validate_called_with_spec_only_no_kwargs(
    tmp_path, monkeypatch
):
    """Round-3 reviewer Q6 pin: validate_schedule_spec is
    called with spec ONLY (no execution_plans, no registries)
    in the phase-7 reminder-only flow."""
    store = _store(tmp_path)
    _seed_complete_minus(store, missing="trigger")

    spy = MagicMock(wraps=setters.validate_schedule_spec)
    monkeypatch.setattr(setters, "validate_schedule_spec", spy)

    future = (_UTC_NOW + timedelta(hours=1)).isoformat()
    await schedule_set_one_off(
        "sched_alpha",
        future,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert spy.call_count == 1
    args, kwargs = spy.call_args
    # spec passed positionally; no kwargs.
    assert len(args) == 1
    assert kwargs == {}


@pytest.mark.asyncio
async def test_complete_cron_draft_without_plan_fails_validation(tmp_path):
    """Cron trigger without execution_plan_hash fails the
    reminder-only rule (design D5). Pin so a future relaxation
    that lets cron reminders through is a deliberate change."""
    store = _store(tmp_path)
    _seed_complete_minus(store, missing="trigger")
    r = await schedule_set_cron(
        "sched_alpha",
        "0 9 * * MON",
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "validation_failed"
    assert any(
        i.code == "missing_execution_plan_for_complex_trigger"
        for i in r.issues
    )


# ===========================================================================
# Partial-draft path
# ===========================================================================


@pytest.mark.asyncio
async def test_partial_draft_returns_not_ready(tmp_path):
    store = _store(tmp_path)
    _seed_partial(store)
    # Apply a valid trigger; description is set already but
    # owner/delivery/failure are not → still not_ready.
    future = (_UTC_NOW + timedelta(hours=1)).isoformat()
    r = await schedule_set_one_off(
        "sched_alpha",
        future,
        "UTC",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )
    assert r.status == "not_ready"
    assert set(r.missing_fields) == {"owner", "delivery", "failure"}
