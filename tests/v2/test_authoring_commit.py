"""Tests for ``app.v2.authoring.commit``.

Phase 8 slice 4 per ``docs/PHASE_8_PLAN.md`` §5.4.

Pins:
- Happy path: complete OneOff + fresh handshake + matching
  hash → ok(schedule_id, spec); row visible in `schedules`;
  schedule_created event visible in `events` with the
  documented payload; draft + handshake files removed.
- Cron-trigger (L87 / Q4): even with fresh matching
  handshake → non_oneoff code. Gate runs BEFORE DB I/O —
  monkeypatched insert_schedule + append_event asserted
  zero calls. Draft + handshake unchanged.
- No handshake → dry_run_required; no row; files unchanged.
- Expired handshake → dry_run_expired; no row.
- Hash drift → body_hash_drift; no row.
- Missing draft → not_found.
- Validation-failing spec → validation_failed; no row.
- Append-event failure rolls back the schedules row;
  files unchanged for retry.
- Duplicate id → validation_failed(duplicate_schedule_id);
  files unchanged.
- schedule_created payload shape: {"hash": spec.hash,
  "template": <name|None>}.

Cleanup-gap regressions (round-1 reviewer L99):
- Draft delete failure post-commit: response stays ok;
  one WARNING entry on `app.v2.authoring.commit`;
  handshake delete still attempted.
- Handshake delete failure post-commit: response stays ok;
  one WARNING entry.
- Both deletes failing: response stays ok; TWO WARNING
  entries.
- Clean commit emits ZERO WARNING entries on the commit
  logger (inverse pin).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.v2.authoring import commit as commit_mod
from app.v2.authoring.commit import schedule_draft_commit
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.handshake import (
    _HANDSHAKE_WINDOW_SECONDS,
    DryRunMode,
    HandshakeStore,
)
from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.triggers import (
    CronTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.storage.events import list_events_for_schedule
from app.v2.storage.schedules import get_schedule


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_EVENT_ID_SEED = "11111111-1111-1111-1111-111111111111"


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _event_id_factory() -> str:
    return _EVENT_ID_SEED


def _migrate_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _stores(tmp_path: Path) -> tuple[DraftStore, HandshakeStore]:
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


async def _seed(tmp_path: Path, draft: ScheduleSpecDraft):
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
async def test_happy_path_inserts_schedule_and_appends_event(tmp_path):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.schedule_id == "sched_alpha"
    assert r.spec is not None
    assert r.spec["id"] == "sched_alpha"

    # Schedule row visible.
    row = get_schedule(conn, "sched_alpha")
    assert row is not None
    assert row.id == "sched_alpha"

    # schedule_created event visible with documented payload.
    events = list_events_for_schedule(conn, "sched_alpha")
    created = [e for e in events if e.kind is EventKind.SCHEDULE_CREATED]
    assert len(created) == 1
    e = created[0]
    assert e.payload == {"hash": row.hash, "template": None}
    assert e.run_id is None
    assert e.id == _EVENT_ID_SEED

    # Cleanup: both files gone.
    with pytest.raises(FileNotFoundError):
        drafts.read("sess1", "sched_alpha")
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_happy_path_with_template_payload_carries_name(tmp_path):
    """spec.template populated → payload.template is the
    name string (Q6)."""
    draft = _oneoff_draft().model_copy(
        update={
            "template": TemplateRef(name="OneOffReminder", version="1"),
        }
    )
    drafts, handshakes = await _seed(tmp_path, draft)
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )
    assert r.status == "ok"

    events = list_events_for_schedule(conn, "sched_alpha")
    created = [e for e in events if e.kind is EventKind.SCHEDULE_CREATED]
    assert created[0].payload["template"] == "OneOffReminder"


# ===========================================================================
# Trigger-type gate (L87 / Q4) — BEFORE DB I/O
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_builder", [_cron_draft, _interval_draft]
)
async def test_non_oneoff_blocked_before_db_io(
    tmp_path, monkeypatch, draft_builder
):
    drafts, handshakes = await _seed(tmp_path, draft_builder())
    conn = _migrate_conn(tmp_path)

    insert_spy = MagicMock(wraps=commit_mod.insert_schedule)
    event_spy = MagicMock(wraps=commit_mod.append_event)
    monkeypatch.setattr(commit_mod, "insert_schedule", insert_spy)
    monkeypatch.setattr(commit_mod, "append_event", event_spy)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "non_oneoff_trigger_blocked_until_real_mode"
        for i in r.issues
    )
    # NO DB I/O.
    assert insert_spy.call_count == 0
    assert event_spy.call_count == 0
    # No row landed.
    assert get_schedule(conn, "sched_alpha") is None
    # Files preserved.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"
    handshakes.read("sess1", "sched_alpha")  # no raise


@pytest.mark.asyncio
async def test_cron_without_plan_hash_returns_non_oneoff_not_validation(
    tmp_path,
):
    """Mirror of the slice-3 reordering pin for commit."""
    drafts, handshakes = _stores(tmp_path)
    bad = _oneoff_draft().model_copy(
        update={
            "trigger": CronTrigger(cron="0 9 * * MON", timezone="UTC"),
            # NO execution_plan_hash.
        }
    )
    drafts.write("sess1", bad)
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    codes = {i.code for i in r.issues}
    assert "non_oneoff_trigger_blocked_until_real_mode" in codes
    assert "missing_execution_plan_for_complex_trigger" not in codes


# ===========================================================================
# Handshake gates — no DB row inserted
# ===========================================================================


@pytest.mark.asyncio
async def test_no_handshake_returns_dry_run_required(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "dry_run_required" for i in r.issues)
    assert get_schedule(conn, "sched_alpha") is None
    # Draft still on disk.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"


@pytest.mark.asyncio
async def test_expired_handshake_returns_dry_run_expired(tmp_path):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    elapsed = 90.0
    later = _UTC_NOW + timedelta(
        seconds=_HANDSHAKE_WINDOW_SECONDS + elapsed
    )

    def _late_clock() -> datetime:
        return later

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_late_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "dry_run_expired" for i in r.issues)
    assert get_schedule(conn, "sched_alpha") is None


@pytest.mark.asyncio
async def test_hash_drift_returns_body_hash_drift(tmp_path):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    drifted = _oneoff_draft().model_copy(
        update={"description": "REVISED weekly amazon summary digest"}
    )
    drafts.write("sess1", drifted)
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "body_hash_drift" for i in r.issues)
    assert get_schedule(conn, "sched_alpha") is None


# ===========================================================================
# Other shapes
# ===========================================================================


@pytest.mark.asyncio
async def test_missing_draft_returns_not_found(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "absent",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
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
    conn = _migrate_conn(tmp_path)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "not_ready"


@pytest.mark.asyncio
async def test_naive_clock_returns_to_spec_failed(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)

    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_naive_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "to_spec_failed" for i in r.issues)


# ===========================================================================
# TX atomicity + duplicate id
# ===========================================================================


@pytest.mark.asyncio
async def test_append_event_failure_rolls_back_insert(
    tmp_path, monkeypatch
):
    """Append-event failure rolls back the insert. Schedule
    row absent + draft + handshake stay on disk."""
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    def _explode(*args, **kwargs):
        raise RuntimeError("simulated append_event failure")

    monkeypatch.setattr(commit_mod, "append_event", _explode)

    with pytest.raises(RuntimeError, match="simulated"):
        await schedule_draft_commit(
            "sched_alpha",
            session_id="sess1",
            store=drafts,
            handshake_store=handshakes,
            conn=conn,
            event_id_factory=_event_id_factory,
            clock=_fixed_clock,
        )

    # Insert rolled back.
    assert get_schedule(conn, "sched_alpha") is None
    # No event row for the schedule.
    assert list_events_for_schedule(conn, "sched_alpha") == []
    # Files preserved.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"
    handshakes.read("sess1", "sched_alpha")  # no raise


@pytest.mark.asyncio
async def test_duplicate_event_id_returns_duplicate_event_id_not_schedule(
    tmp_path,
):
    """Reviewer slice-4 verdict: event-id collision must
    surface as `duplicate_event_id`, NOT
    `duplicate_schedule_id`. Pre-fix the single outer
    `except sqlite3.IntegrityError` mis-attributed the
    event collision as a schedule-id duplicate; pin the
    discrimination."""
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    # Pre-seed an event row with the id the factory will
    # return. Use a separate dummy schedule so the FK is
    # satisfied. (Events.schedule_id has an FK to
    # schedules.id; we land a sentinel schedule first.)
    from app.v2.models.event import Event as _Event
    from app.v2.models.triggers import OneOffTrigger as _OneOff

    sentinel_draft = _oneoff_draft(id_="sentinel_for_event_seed")
    sentinel_drafts, sentinel_handshakes = await _seed(
        tmp_path, sentinel_draft
    )
    r0 = await schedule_draft_commit(
        "sentinel_for_event_seed",
        session_id="sess1",
        store=sentinel_drafts,
        handshake_store=sentinel_handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )
    assert r0.status == "ok"

    # Now sched_alpha is brand-new. Its event_id_factory
    # returns the SAME id already in the events table from
    # the sentinel commit above → events.id PRIMARY KEY
    # collision on append_event.
    r = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    codes = {i.code for i in r.issues}
    assert "duplicate_event_id" in codes
    # CRITICAL pin: must NOT mis-attribute as schedule
    # duplicate.
    assert "duplicate_schedule_id" not in codes
    # Schedule row rolled back (TX atomicity).
    assert get_schedule(conn, "sched_alpha") is None
    # Files preserved for retry.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"
    handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_duplicate_id_returns_validation_failed(tmp_path):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    # First commit succeeds.
    r1 = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=_event_id_factory,
        clock=_fixed_clock,
    )
    assert r1.status == "ok"

    # Re-seed for the second attempt (first commit deleted
    # the files).
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())

    r2 = await schedule_draft_commit(
        "sched_alpha",
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        conn=conn,
        event_id_factory=lambda: "22222222-2222-2222-2222-222222222222",
        clock=_fixed_clock,
    )

    assert r2.status == "validation_failed"
    assert any(i.code == "duplicate_schedule_id" for i in r2.issues)
    # Files preserved for forensics / retry.
    assert drafts.read("sess1", "sched_alpha").id == "sched_alpha"
    handshakes.read("sess1", "sched_alpha")


# ===========================================================================
# Post-commit cleanup (round-1 reviewer L99)
# ===========================================================================


@pytest.mark.asyncio
async def test_draft_delete_failure_logs_warning_response_ok(
    tmp_path, monkeypatch, caplog
):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    def _boom(session_id, draft_id):
        raise OSError("simulated draft delete failure")

    monkeypatch.setattr(drafts, "delete", _boom)

    with caplog.at_level(logging.WARNING, logger="app.v2.authoring.commit"):
        r = await schedule_draft_commit(
            "sched_alpha",
            session_id="sess1",
            store=drafts,
            handshake_store=handshakes,
            conn=conn,
            event_id_factory=_event_id_factory,
            clock=_fixed_clock,
        )

    assert r.status == "ok"
    # Schedule + event present.
    assert get_schedule(conn, "sched_alpha") is not None
    assert any(
        e.kind is EventKind.SCHEDULE_CREATED
        for e in list_events_for_schedule(conn, "sched_alpha")
    )
    # Exactly one WARNING on the commit logger naming the
    # schedule id.
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.v2.authoring.commit"
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "sched_alpha" in warnings[0].getMessage()
    # Handshake delete still attempted (file gone).
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_handshake_delete_failure_logs_warning_response_ok(
    tmp_path, monkeypatch, caplog
):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    def _boom(session_id, draft_id):
        raise OSError("simulated handshake delete failure")

    monkeypatch.setattr(handshakes, "delete", _boom)

    with caplog.at_level(logging.WARNING, logger="app.v2.authoring.commit"):
        r = await schedule_draft_commit(
            "sched_alpha",
            session_id="sess1",
            store=drafts,
            handshake_store=handshakes,
            conn=conn,
            event_id_factory=_event_id_factory,
            clock=_fixed_clock,
        )

    assert r.status == "ok"
    assert get_schedule(conn, "sched_alpha") is not None
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.v2.authoring.commit"
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "sched_alpha" in warnings[0].getMessage()
    # Draft deletion succeeded.
    with pytest.raises(FileNotFoundError):
        drafts.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_both_deletes_failing_logs_two_warnings_response_ok(
    tmp_path, monkeypatch, caplog
):
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    monkeypatch.setattr(
        drafts,
        "delete",
        lambda s, d: (_ for _ in ()).throw(OSError("draft fail")),
    )
    monkeypatch.setattr(
        handshakes,
        "delete",
        lambda s, d: (_ for _ in ()).throw(OSError("handshake fail")),
    )

    with caplog.at_level(logging.WARNING, logger="app.v2.authoring.commit"):
        r = await schedule_draft_commit(
            "sched_alpha",
            session_id="sess1",
            store=drafts,
            handshake_store=handshakes,
            conn=conn,
            event_id_factory=_event_id_factory,
            clock=_fixed_clock,
        )

    assert r.status == "ok"
    assert get_schedule(conn, "sched_alpha") is not None
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.v2.authoring.commit"
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_clean_commit_emits_zero_warnings(
    tmp_path, caplog
):
    """Inverse pin: the best-effort branch only logs on
    failure. A clean commit emits ZERO WARNING records on
    the commit logger."""
    drafts, handshakes = await _seed(tmp_path, _oneoff_draft())
    conn = _migrate_conn(tmp_path)

    with caplog.at_level(logging.WARNING, logger="app.v2.authoring.commit"):
        r = await schedule_draft_commit(
            "sched_alpha",
            session_id="sess1",
            store=drafts,
            handshake_store=handshakes,
            conn=conn,
            event_id_factory=_event_id_factory,
            clock=_fixed_clock,
        )

    assert r.status == "ok"
    warnings = [
        r
        for r in caplog.records
        if r.name == "app.v2.authoring.commit"
        and r.levelno == logging.WARNING
    ]
    assert warnings == []


# ===========================================================================
# Module hygiene
# ===========================================================================


def test_commit_module_does_not_bind_prod_clock():
    assert not hasattr(commit_mod, "prod_clock")


def test_commit_module_does_not_bind_uuid4_factory():
    """Phase-5 rule 10: event_id_factory is DI; the module
    does not import uuid."""
    import inspect

    src = inspect.getsource(commit_mod)
    assert "uuid4" not in src


def test_commit_body_calls_no_datetime_now():
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(schedule_draft_commit))
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
    assert not leaked, (
        f"schedule_draft_commit must be clock-free; got {leaked!r}"
    )
