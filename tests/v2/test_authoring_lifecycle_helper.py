"""Tests for ``app.v2.authoring.lifecycle_helper``.

Phase 7 slice 5a per ``docs/PHASE_7_PLAN.md`` §5.6a.

Pins:
- ``_ACTION_TO_STATUS_AND_EVENT`` maps each
  ``_LifecycleAction`` to exactly one
  ``(ScheduleStatus, EventKind)`` pair (L144 mismatch
  impossible).
- update_status_with_event flips status + appends event in
  one TX. Missing schedule_id → ScheduleNotFoundError.
- No-op semantics (L544 / Q15): already-in-target-status
  performs no update, appends no event.
- Rollback on event-insert failure: schedules row unchanged;
  no event row.
- Archive variant cancels every pending Run + appends a
  ``run_cancelled`` event per Run in one TX.
- Archive variant: non-pending Runs (running / claimed /
  succeeded / failed) NOT touched.
- Archive cancel branch routes through
  ``assert_legal_transition(PENDING, CANCELLED)`` — pin via
  temporarily removing the entry and observing
  IllegalTransitionError.
- ``(PENDING, CANCELLED)`` in LEGAL_TRANSITIONS (L155 plan
  guarantee).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable
from unittest.mock import MagicMock

import pytest

from app.v2.authoring import lifecycle_helper as helper_mod
from app.v2.authoring.lifecycle_helper import (
    _ACTION_TO_STATUS_AND_EVENT,
    _LifecycleAction,
    update_status_with_event,
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
from app.v2.runtime.state_machine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
)
from app.v2.storage.runs import insert_run
from app.v2.storage.schedules import (
    ScheduleNotFoundError,
    insert_schedule,
)


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
    """Build a migrated in-memory-ish SQLite connection."""
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


# ===========================================================================
# _ACTION_TO_STATUS_AND_EVENT mapping (L144)
# ===========================================================================


def test_action_mapping_covers_all_four_actions():
    assert set(_ACTION_TO_STATUS_AND_EVENT.keys()) == {
        _LifecycleAction.PAUSE,
        _LifecycleAction.RESUME,
        _LifecycleAction.ARCHIVE,
        _LifecycleAction.REVIVE,
    }


@pytest.mark.parametrize(
    "action, target_status, event_kind",
    [
        (
            _LifecycleAction.PAUSE,
            ScheduleStatus.PAUSED,
            EventKind.SCHEDULE_PAUSED,
        ),
        (
            _LifecycleAction.RESUME,
            ScheduleStatus.ACTIVE,
            EventKind.SCHEDULE_RESUMED,
        ),
        (
            _LifecycleAction.ARCHIVE,
            ScheduleStatus.ARCHIVED,
            EventKind.SCHEDULE_ARCHIVED,
        ),
        (
            _LifecycleAction.REVIVE,
            ScheduleStatus.PAUSED,  # round-2 L442 gate
            EventKind.SCHEDULE_REVIVED,
        ),
    ],
)
def test_action_mapping_produces_exact_pair(
    action, target_status, event_kind
):
    assert _ACTION_TO_STATUS_AND_EVENT[action] == (
        target_status,
        event_kind,
    )


# ===========================================================================
# State machine new entry (L155 plan guarantee)
# ===========================================================================


def test_pending_to_cancelled_is_legal_transition():
    assert (
        RunStatus.PENDING,
        RunStatus.CANCELLED,
    ) in LEGAL_TRANSITIONS


# ===========================================================================
# Happy path — status flip + event append
# ===========================================================================


def test_pause_flips_status_and_appends_event(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.PAUSE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    conn.commit()

    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.PAUSED.value

    events = conn.execute(
        "SELECT kind FROM events WHERE schedule_id = ?",
        ("sched_alpha",),
    ).fetchall()
    assert [e[0] for e in events] == [
        EventKind.SCHEDULE_PAUSED.value
    ]


def test_resume_flips_paused_to_active(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.PAUSED)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.RESUME,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    conn.commit()

    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.ACTIVE.value

    kinds = [
        r[0]
        for r in conn.execute(
            "SELECT kind FROM events WHERE schedule_id = ?",
            ("sched_alpha",),
        ).fetchall()
    ]
    assert kinds == [EventKind.SCHEDULE_RESUMED.value]


def test_revive_flips_archived_to_paused(tmp_path):
    """L442 gate: archived → PAUSED, not active."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ARCHIVED)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.REVIVE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    conn.commit()

    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.PAUSED.value


# ===========================================================================
# Missing schedule_id
# ===========================================================================


def test_missing_schedule_id_raises(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ScheduleNotFoundError):
        update_status_with_event(
            conn,
            "no_such",
            _LifecycleAction.PAUSE,
            event_id_factory=_counter_event_factory(),
            clock=_fixed_clock,
        )


# ===========================================================================
# No-op semantics (L544 / Q15)
# ===========================================================================


def test_pause_on_already_paused_is_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.PAUSED)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.PAUSE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    conn.commit()

    # Status unchanged.
    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.PAUSED.value
    # No event appended.
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert count == 0


def test_resume_on_already_active_is_noop(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.RESUME,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    conn.commit()

    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.ACTIVE.value
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert count == 0


# ===========================================================================
# Rollback on event-insert failure
# ===========================================================================


def test_rollback_on_event_insert_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    def _boom(_conn, _event):
        raise RuntimeError("simulated event insert failure")

    monkeypatch.setattr(helper_mod, "append_event", _boom)

    with pytest.raises(RuntimeError, match="simulated event"):
        update_status_with_event(
            conn,
            "sched_alpha",
            _LifecycleAction.PAUSE,
            event_id_factory=_counter_event_factory(),
            clock=_fixed_clock,
        )
    # Status NOT changed.
    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.ACTIVE.value
    # No event row landed.
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert count == 0


# ===========================================================================
# Archive cancellation branch
# ===========================================================================


def test_archive_cancels_pending_runs(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    for i in range(3):
        _seed_run(
            conn,
            schedule_id="sched_alpha",
            run_id=f"run_{i:02d}",
            status=RunStatus.PENDING,
        )

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.ARCHIVE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
        cancel_pending_runs=True,
    )
    conn.commit()

    # All three Runs flipped to cancelled.
    statuses = {
        r[0]
        for r in conn.execute(
            "SELECT status FROM runs WHERE schedule_id = ?",
            ("sched_alpha",),
        ).fetchall()
    }
    assert statuses == {RunStatus.CANCELLED.value}

    # One schedule_archived + three run_cancelled events.
    kinds = sorted(
        r[0]
        for r in conn.execute(
            "SELECT kind FROM events WHERE schedule_id = ?",
            ("sched_alpha",),
        ).fetchall()
    )
    assert kinds == sorted(
        [
            EventKind.SCHEDULE_ARCHIVED.value,
            EventKind.RUN_CANCELLED.value,
            EventKind.RUN_CANCELLED.value,
            EventKind.RUN_CANCELLED.value,
        ]
    )


def test_archive_does_not_touch_non_pending_runs(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    _seed_run(
        conn,
        schedule_id="sched_alpha",
        run_id="run_pending",
        status=RunStatus.PENDING,
    )
    _seed_run(
        conn,
        schedule_id="sched_alpha",
        run_id="run_claimed",
        status=RunStatus.CLAIMED,
    )
    _seed_run(
        conn,
        schedule_id="sched_alpha",
        run_id="run_succeeded",
        status=RunStatus.SUCCEEDED,
    )

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.ARCHIVE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
        cancel_pending_runs=True,
    )
    conn.commit()

    rows = conn.execute(
        "SELECT id, status FROM runs WHERE schedule_id = ? ORDER BY id",
        ("sched_alpha",),
    ).fetchall()
    statuses = dict(rows)
    assert statuses["run_pending"] == RunStatus.CANCELLED.value
    # Untouched.
    assert statuses["run_claimed"] == RunStatus.CLAIMED.value
    assert statuses["run_succeeded"] == RunStatus.SUCCEEDED.value


def test_archive_run_cancelled_event_correlates_with_schedule_event(
    tmp_path,
):
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    _seed_run(
        conn,
        schedule_id="sched_alpha",
        run_id="run_pending",
        status=RunStatus.PENDING,
    )

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.ARCHIVE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
        cancel_pending_runs=True,
    )
    conn.commit()

    sched_event_id = conn.execute(
        "SELECT id FROM events WHERE kind = ?",
        (EventKind.SCHEDULE_ARCHIVED.value,),
    ).fetchone()[0]
    run_event_correlates = conn.execute(
        "SELECT correlates FROM events WHERE kind = ?",
        (EventKind.RUN_CANCELLED.value,),
    ).fetchone()[0]
    assert run_event_correlates == sched_event_id


# ===========================================================================
# Chokepoint routing pin (L155)
# ===========================================================================


def test_archive_cancel_branch_routes_through_assert_legal_transition(
    tmp_path, monkeypatch
):
    """Round-3 reviewer L155 pin: even though we know
    src=PENDING and dst=CANCELLED, the helper must call
    assert_legal_transition() so the runtime state-machine
    chokepoint owns the policy. Temporarily remove the
    transition and confirm the helper raises."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)
    _seed_run(
        conn,
        schedule_id="sched_alpha",
        run_id="run_pending",
        status=RunStatus.PENDING,
    )

    # Replace LEGAL_TRANSITIONS via the module's `is_legal_transition`
    # / `assert_legal_transition` import in helper_mod.
    from app.v2.runtime import state_machine as sm

    original_legal = sm.LEGAL_TRANSITIONS
    try:
        sm.LEGAL_TRANSITIONS = frozenset(
            t
            for t in original_legal
            if t != (RunStatus.PENDING, RunStatus.CANCELLED)
        )
        with pytest.raises(IllegalTransitionError):
            update_status_with_event(
                conn,
                "sched_alpha",
                _LifecycleAction.ARCHIVE,
                event_id_factory=_counter_event_factory(),
                clock=_fixed_clock,
                cancel_pending_runs=True,
            )
    finally:
        sm.LEGAL_TRANSITIONS = original_legal


def test_cancel_branch_no_pending_runs_succeeds(tmp_path):
    """If the schedule has no pending Runs at archive time,
    the helper still flips status + appends schedule_archived
    + returns cleanly."""
    conn = _conn(tmp_path)
    _seed_schedule(conn, status=ScheduleStatus.ACTIVE)

    update_status_with_event(
        conn,
        "sched_alpha",
        _LifecycleAction.ARCHIVE,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
        cancel_pending_runs=True,
    )
    conn.commit()

    status = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert status == ScheduleStatus.ARCHIVED.value
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ?",
        ("sched_alpha",),
    ).fetchone()[0]
    assert count == 1  # only schedule_archived
