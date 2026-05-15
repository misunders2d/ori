"""Tests for ``app.v2.authoring.freeze``.

Phase 8 slice 3 per ``docs/PHASE_8_PLAN.md`` §5.3.

Pins:
- Fresh handshake matching current OneOff draft → ok with
  spec.
- Cron-trigger draft (L87 / Q4): even with a fresh matching
  handshake → validation_failed(
  non_oneoff_trigger_blocked_until_real_mode). Same code
  surfaces with no handshake at all (gate runs BEFORE the
  handshake check).
- Interval-trigger draft → same code as cron.
- No handshake (OneOff) → validation_failed(dry_run_required).
- Expired handshake → validation_failed(dry_run_expired)
  with elapsed seconds in message.
- Hash drift → validation_failed(body_hash_drift).
- Missing draft → not_found.
- Incomplete draft → not_ready.
- Naive clock → to_spec_failed.
- Freeze does NOT touch v2 DB — module source has no
  insert_schedule / append_event reference; no DB
  fixture wired.
- Freeze does NOT delete draft file or handshake file —
  post-call reads still succeed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v2.authoring import freeze as freeze_mod
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.freeze import schedule_freeze
from app.v2.authoring.handshake import (
    _HANDSHAKE_WINDOW_SECONDS,
    DryRunMode,
    HandshakeRecord,
    HandshakeStore,
)
from app.v2.enums import DeliveryFallbackPolicy, FailureActionType
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.triggers import (
    CronTrigger,
    IntervalTrigger,
    OneOffTrigger,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _stores(tmp_path) -> tuple[DraftStore, HandshakeStore]:
    return (
        DraftStore(base=tmp_path / "drafts"),
        HandshakeStore(base=tmp_path / "handshakes"),
    )


def _oneoff_draft(id_="sched_alpha") -> ScheduleSpecDraft:
    return ScheduleSpecDraft(
        id=id_,
        description="weekly amazon summary digest",
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        trigger=OneOffTrigger(
            at_iso_datetime=_UTC_NOW + timedelta(days=1),
            timezone="UTC",
        ),
        delivery=Delivery(
            target_session_id="C123",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
    )


def _cron_draft(id_="sched_alpha") -> ScheduleSpecDraft:
    """Cron draft with a syntactically valid execution_plan_hash
    so it passes the reminder-only rule and exposes the L87
    trigger-type gate."""
    return _oneoff_draft(id_=id_).model_copy(
        update={
            "trigger": CronTrigger(cron="0 9 * * MON", timezone="UTC"),
            "execution_plan_hash": "a" * 64,
        }
    )


def _interval_draft(id_="sched_alpha") -> ScheduleSpecDraft:
    return _oneoff_draft(id_=id_).model_copy(
        update={
            "trigger": IntervalTrigger(every_seconds=3600),
            "execution_plan_hash": "b" * 64,
        }
    )


async def _seed_dry_run(
    tmp_path, draft: ScheduleSpecDraft
) -> tuple[DraftStore, HandshakeStore]:
    """Write the draft + record a fresh handshake via the
    real schedule_dry_run path so body_hash matches the spec
    canonical hash."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", draft)
    r = await schedule_dry_run(
        draft.id,
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )
    assert r.status == "ok", f"dry-run seed failed: {r!r}"
    return drafts, handshakes


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_fresh_handshake_oneoff_returns_ok_with_spec(tmp_path):
    drafts, handshakes = await _seed_dry_run(tmp_path, _oneoff_draft())

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert r.spec is not None
    assert r.spec["id"] == "sched_alpha"
    assert r.spec["hash"]


# ===========================================================================
# Trigger-type gate (L87 / Q4)
# ===========================================================================


@pytest.mark.asyncio
async def test_cron_with_fresh_handshake_blocked(tmp_path):
    """Cron + fresh matching handshake → non_oneoff code."""
    drafts, handshakes = await _seed_dry_run(tmp_path, _cron_draft())

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "non_oneoff_trigger_blocked_until_real_mode"
        for i in r.issues
    )


@pytest.mark.asyncio
async def test_cron_without_handshake_returns_non_oneoff_not_dry_run_required(
    tmp_path,
):
    """L87 gate runs BEFORE the handshake check; a cron draft
    with NO handshake at all still surfaces the non_oneoff
    code, not dry_run_required."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _cron_draft())

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    codes = {i.code for i in r.issues}
    assert "non_oneoff_trigger_blocked_until_real_mode" in codes
    assert "dry_run_required" not in codes


@pytest.mark.asyncio
async def test_cron_without_plan_hash_returns_non_oneoff_not_validation_failure(
    tmp_path,
):
    """Reviewer slice-3 verdict: gate ordering bug — if the
    non-OneOff gate runs AFTER validate_schedule_spec, a
    cron draft without execution_plan_hash trips the
    reminder-only rule (`missing_execution_plan_for_complex_
    trigger`) and masks the L87 code the LLM needs to see.

    Pin: cron + no execution_plan_hash + no handshake →
    non_oneoff_trigger_blocked_until_real_mode. Pre-fix this
    asserted the validation code; post-fix it asserts the
    non-OneOff code per plan §3.3 + Q4."""
    drafts, handshakes = _stores(tmp_path)
    bad = _oneoff_draft().model_copy(
        update={
            "trigger": CronTrigger(cron="0 9 * * MON", timezone="UTC"),
            # NO execution_plan_hash — would trip reminder-only
            # rule if the gate ran after validate_schedule_spec.
        }
    )
    drafts.write("sess1", bad)

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    codes = {i.code for i in r.issues}
    assert "non_oneoff_trigger_blocked_until_real_mode" in codes
    # The masked validation code must NOT appear — gate
    # short-circuited before the validation chokepoint.
    assert "missing_execution_plan_for_complex_trigger" not in codes


@pytest.mark.asyncio
async def test_interval_with_fresh_handshake_blocked(tmp_path):
    drafts, handshakes = await _seed_dry_run(tmp_path, _interval_draft())

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "non_oneoff_trigger_blocked_until_real_mode"
        for i in r.issues
    )


# ===========================================================================
# Handshake gates
# ===========================================================================


@pytest.mark.asyncio
async def test_oneoff_no_handshake_returns_dry_run_required(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _oneoff_draft())

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "dry_run_required" for i in r.issues)
    assert any("schedule_dry_run" in i.message for i in r.issues)


@pytest.mark.asyncio
async def test_expired_handshake_returns_dry_run_expired_with_elapsed(
    tmp_path,
):
    drafts, handshakes = await _seed_dry_run(tmp_path, _oneoff_draft())

    # Advance clock past the 60s freeze window.
    elapsed = 90.0  # seconds past expiry
    later = _UTC_NOW + timedelta(seconds=_HANDSHAKE_WINDOW_SECONDS + elapsed)

    def _late_clock() -> datetime:
        return later

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_late_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "dry_run_expired" for i in r.issues)
    # Elapsed value appears in the message.
    assert any(f"{elapsed:.1f}" in i.message for i in r.issues)


@pytest.mark.asyncio
async def test_hash_drift_returns_body_hash_drift(tmp_path):
    """Author mutates the draft after the dry-run. Freeze must
    surface body_hash_drift, not silently re-record."""
    drafts, handshakes = await _seed_dry_run(tmp_path, _oneoff_draft())

    # Mutate the draft on disk (a different description →
    # different canonical hash).
    drift_draft = _oneoff_draft().model_copy(
        update={"description": "REVISED weekly amazon summary digest"}
    )
    drafts.write("sess1", drift_draft)

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "body_hash_drift" for i in r.issues)


# ===========================================================================
# not_found / not_ready / naive clock
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_draft_returns_not_found(tmp_path):
    drafts, handshakes = _stores(tmp_path)

    r = await schedule_freeze(
        "absent",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "not_found"


@pytest.mark.asyncio
async def test_incomplete_draft_returns_not_ready(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write(
        "sess1",
        ScheduleSpecDraft(
            id="sched_alpha", description="weekly digest"
        ),
    )

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "not_ready"
    assert set(r.missing_fields) == {
        "owner",
        "trigger",
        "delivery",
        "failure",
    }


@pytest.mark.asyncio
async def test_naive_clock_returns_to_spec_failed(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _oneoff_draft())

    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)  # no tzinfo

    r = await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_naive_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "to_spec_failed" for i in r.issues)


# ===========================================================================
# Freeze immutability
# ===========================================================================


@pytest.mark.asyncio
async def test_freeze_does_not_delete_draft_file(tmp_path):
    drafts, handshakes = await _seed_dry_run(tmp_path, _oneoff_draft())

    await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    # Draft still loadable.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"


@pytest.mark.asyncio
async def test_freeze_does_not_delete_handshake_file(tmp_path):
    drafts, handshakes = await _seed_dry_run(tmp_path, _oneoff_draft())

    await schedule_freeze(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    # Handshake still loadable.
    record = handshakes.read("sess1", "sched_alpha")
    assert isinstance(record, HandshakeRecord)


def test_freeze_module_does_not_import_storage_insert():
    """Freeze must NOT call insert_schedule or append_event.
    Pin via grep on the module source."""
    import inspect

    src = inspect.getsource(freeze_mod)
    assert "insert_schedule" not in src
    assert "append_event" not in src


# ===========================================================================
# Module hygiene
# ===========================================================================


def test_freeze_module_does_not_bind_prod_clock():
    assert not hasattr(freeze_mod, "prod_clock")


def test_freeze_body_calls_no_datetime_now():
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(schedule_freeze))
    tree = ast.parse(source)
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                calls.add(".".join(reversed(parts)))

    forbidden = {"datetime.now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, f"schedule_freeze must be clock-free; got {leaked!r}"
