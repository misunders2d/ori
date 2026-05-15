"""Tests for ``app.v2.authoring.lifecycle``.

Phase 7 slice 5b per ``docs/PHASE_7_PLAN.md`` §5.6b.

Pins:
- Each tool returns ok on success (with schedule_id) /
  not_found on missing.
- pause / archive / revive / resume schedule + event written
  via the helper; query DB to verify.
- already-paused → pause returns ok with "already paused"
  hint + no event added (L544 / Q15).
- archived → resume returns validation_failed naming the
  revive-first requirement (round-2 L442 gate).
- archive cancels every pending Run; payload reports
  count via message.
- revive flips archived → PAUSED; subsequent resume flips
  PAUSED → active.
- Phase-5 lifecycle hooks (app.v2.runtime.lifecycle.*) are
  NOT called by phase-7 tools — pin via patching all four
  hooks and asserting zero calls.
- event_id_factory required (no default); distinct ids per
  call.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from typing import Callable
from unittest.mock import patch

import pytest

from app.v2.authoring.lifecycle import (
    schedule_archive,
    schedule_pause,
    schedule_resume,
    schedule_revive,
)
from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    FireReason,
    RunStatus,
    ScheduleStatus,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.run import Run
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.storage.runs import insert_run
from app.v2.storage.schedules import insert_schedule


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _counter_event_factory() -> Callable[[], str]:
    state = {"i": 0}

    def _next() -> str:
        state["i"] += 1
        return f"evt-{state['i']:04d}"

    return _next


def _conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(str(db_path))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(
    conn: sqlite3.Connection,
    *,
    schedule_id: str = "sched_alpha",
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> ScheduleSpec:
    spec = ScheduleSpec(
        id=schedule_id,
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        description="weekly amazon summary digest",
        trigger=OneOffTrigger(at_iso_datetime=_UTC_NOW, timezone="UTC"),
        delivery=Delivery(
            target_session_id="C123",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
        status=status,
        authored_at=_UTC_NOW.isoformat(),
    ).with_fresh_hash()
    insert_schedule(conn, spec)
    conn.commit()
    return spec


def _seed_run(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    run_id: str,
    status: RunStatus = RunStatus.PENDING,
) -> Run:
    run = Run(
        id=run_id,
        schedule_id=schedule_id,
        fire_reason=FireReason.SCHEDULED,
        due_at=_UTC_NOW,
        status=status,
        root_run_id=run_id,
    )
    insert_run(conn, run)
    conn.commit()
    return run


def _status_of(conn, schedule_id):
    return conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()[0]


def _event_kinds_for(conn, schedule_id):
    return [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM events WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchall()
    ]


# ===========================================================================
# schedule_pause
# ===========================================================================


@pytest.mark.asyncio
async def test_pause_active_succeeds(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    r = await schedule_pause(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert r.schedule_id == "sched_alpha"
    assert _status_of(conn, "sched_alpha") == ScheduleStatus.PAUSED.value
    assert _event_kinds_for(conn, "sched_alpha") == [
        EventKind.SCHEDULE_PAUSED.value
    ]


@pytest.mark.asyncio
async def test_pause_already_paused_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.PAUSED)

    r = await schedule_pause(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "already paused" in r.message
    assert _event_kinds_for(conn, "sched_alpha") == []


@pytest.mark.asyncio
async def test_pause_missing_schedule(tmp_path):
    conn = _conn(tmp_path)

    r = await schedule_pause(
        "absent",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "not_found"


# ===========================================================================
# schedule_resume
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_paused_succeeds(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.PAUSED)

    r = await schedule_resume(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert _status_of(conn, "sched_alpha") == ScheduleStatus.ACTIVE.value
    assert _event_kinds_for(conn, "sched_alpha") == [
        EventKind.SCHEDULE_RESUMED.value
    ]


@pytest.mark.asyncio
async def test_resume_archived_blocked(tmp_path):
    """Round-2 L442 gate: archived → must revive first."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ARCHIVED)

    r = await schedule_resume(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    codes = [i.code for i in r.issues]
    assert "resume_from_archived_blocked" in codes
    msg = r.issues[0].message
    assert "schedule_revive" in msg
    # Status unchanged.
    assert (
        _status_of(conn, "sched_alpha") == ScheduleStatus.ARCHIVED.value
    )


@pytest.mark.asyncio
async def test_resume_already_active_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    r = await schedule_resume(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "already active" in r.message
    assert _event_kinds_for(conn, "sched_alpha") == []


@pytest.mark.asyncio
async def test_resume_missing_schedule(tmp_path):
    conn = _conn(tmp_path)

    r = await schedule_resume(
        "absent",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "not_found"


# ===========================================================================
# schedule_archive
# ===========================================================================


@pytest.mark.asyncio
async def test_archive_active_cancels_pending_runs(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    for i in range(2):
        _seed_run(
            conn,
            schedule_id="sched_alpha",
            run_id=f"run_{i:02d}",
            status=RunStatus.PENDING,
        )

    r = await schedule_archive(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "cancelled 2" in r.message
    assert (
        _status_of(conn, "sched_alpha") == ScheduleStatus.ARCHIVED.value
    )
    run_statuses = {
        r[0]
        for r in conn.execute(
            "SELECT status FROM runs WHERE schedule_id = ?",
            ("sched_alpha",),
        ).fetchall()
    }
    assert run_statuses == {RunStatus.CANCELLED.value}


@pytest.mark.asyncio
async def test_archive_zero_pending_runs(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    r = await schedule_archive(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "cancelled 0" in r.message


@pytest.mark.asyncio
async def test_archive_already_archived_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ARCHIVED)

    r = await schedule_archive(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "already archived" in r.message
    assert _event_kinds_for(conn, "sched_alpha") == []


@pytest.mark.asyncio
async def test_archive_missing_schedule(tmp_path):
    conn = _conn(tmp_path)

    r = await schedule_archive(
        "absent",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "not_found"


# ===========================================================================
# schedule_revive
# ===========================================================================


@pytest.mark.asyncio
async def test_revive_archived_flips_to_paused(tmp_path):
    """Round-2 L442: archived → PAUSED (not active)."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ARCHIVED)

    r = await schedule_revive(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert _status_of(conn, "sched_alpha") == ScheduleStatus.PAUSED.value
    assert _event_kinds_for(conn, "sched_alpha") == [
        EventKind.SCHEDULE_REVIVED.value
    ]


@pytest.mark.asyncio
async def test_revive_then_resume_two_step_flow(tmp_path):
    """L442 two-step gate: revive flips archived → PAUSED;
    a subsequent resume then flips PAUSED → active. Pin so
    the gate cannot be bypassed in one step."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ARCHIVED)
    event_factory = _counter_event_factory()

    revive_r = await schedule_revive(
        "sched_alpha",
        conn=conn,
        event_id_factory=event_factory,
        clock=_fixed_clock,
    )
    assert revive_r.status == "ok"
    assert _status_of(conn, "sched_alpha") == ScheduleStatus.PAUSED.value

    resume_r = await schedule_resume(
        "sched_alpha",
        conn=conn,
        event_id_factory=event_factory,
        clock=_fixed_clock,
    )
    assert resume_r.status == "ok"
    assert _status_of(conn, "sched_alpha") == ScheduleStatus.ACTIVE.value
    assert _event_kinds_for(conn, "sched_alpha") == [
        EventKind.SCHEDULE_REVIVED.value,
        EventKind.SCHEDULE_RESUMED.value,
    ]


@pytest.mark.asyncio
async def test_revive_already_paused_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.PAUSED)

    r = await schedule_revive(
        "sched_alpha",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert "already paused" in r.message
    assert _event_kinds_for(conn, "sched_alpha") == []


@pytest.mark.asyncio
async def test_revive_missing_schedule(tmp_path):
    conn = _conn(tmp_path)

    r = await schedule_revive(
        "absent",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )

    assert r.status == "not_found"


# ===========================================================================
# Phase-5 lifecycle hooks NOT called (round-2 L598 separation)
# ===========================================================================


@pytest.mark.asyncio
async def test_phase5_hooks_not_called_by_authoring_tools(tmp_path):
    """The phase-5 binding lifecycle hooks
    (app.v2.runtime.lifecycle.on_schedule_*) are NOT called
    by phase-7 authoring tools; the APScheduler binding is
    not mounted in phase 7. Phase-9 cutover wires the hook
    calls alongside these tools."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    with patch(
        "app.v2.runtime.lifecycle.on_schedule_paused"
    ) as on_paused, patch(
        "app.v2.runtime.lifecycle.on_schedule_resumed"
    ) as on_resumed, patch(
        "app.v2.runtime.lifecycle.on_schedule_archived"
    ) as on_archived, patch(
        "app.v2.runtime.lifecycle.on_schedule_revised"
    ) as on_revised:
        event_factory = _counter_event_factory()
        await schedule_pause(
            "sched_alpha",
            conn=conn,
            event_id_factory=event_factory,
            clock=_fixed_clock,
        )
        await schedule_resume(
            "sched_alpha",
            conn=conn,
            event_id_factory=event_factory,
            clock=_fixed_clock,
        )
        await schedule_archive(
            "sched_alpha",
            conn=conn,
            event_id_factory=event_factory,
            clock=_fixed_clock,
        )
        await schedule_revive(
            "sched_alpha",
            conn=conn,
            event_id_factory=event_factory,
            clock=_fixed_clock,
        )

        assert on_paused.call_count == 0
        assert on_resumed.call_count == 0
        assert on_archived.call_count == 0
        assert on_revised.call_count == 0


# ===========================================================================
# event_id_factory required (no default) + distinct ids
# ===========================================================================


@pytest.mark.parametrize(
    "fn",
    [
        schedule_pause,
        schedule_resume,
        schedule_archive,
        schedule_revive,
    ],
)
def test_event_id_factory_is_required_keyword_only(fn):
    sig = inspect.signature(fn)
    p = sig.parameters["event_id_factory"]
    assert p.default is inspect.Parameter.empty
    assert p.kind == inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_distinct_event_ids_across_lifecycle_calls(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    event_factory = _counter_event_factory()

    await schedule_pause(
        "sched_alpha",
        conn=conn,
        event_id_factory=event_factory,
        clock=_fixed_clock,
    )
    await schedule_resume(
        "sched_alpha",
        conn=conn,
        event_id_factory=event_factory,
        clock=_fixed_clock,
    )

    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM events WHERE schedule_id = ?",
            ("sched_alpha",),
        ).fetchall()
    ]
    assert len(set(ids)) == len(ids), "event ids must be distinct"


# ===========================================================================
# Clock parameter required + keyword-only
# ===========================================================================


@pytest.mark.parametrize(
    "fn",
    [
        schedule_pause,
        schedule_resume,
        schedule_archive,
        schedule_revive,
    ],
)
def test_clock_is_required_keyword_only(fn):
    sig = inspect.signature(fn)
    p = sig.parameters["clock"]
    assert p.default is inspect.Parameter.empty
    assert p.kind == inspect.Parameter.KEYWORD_ONLY
