"""V2 scheduler — atomic status-flip + EventLedger helper.

Phase 7 slice 5a per ``docs/PHASE_7_PLAN.md`` §3.6 + §5.6a.

One helper — :func:`update_status_with_event` — flips a
schedule's status AND appends the matching EventLedger entry
in a single transaction. The action argument drives BOTH the
target status and the event kind via a fixed mapping so a
caller cannot pass an inconsistent pair (round-2 reviewer
L144).

When the ``cancel_pending_runs`` flag is set (used only by
the archive lifecycle tool in slice 5b), the helper ALSO
flips every pending Run for the schedule to ``cancelled`` and
appends a ``run_cancelled`` event for each. Each pending →
cancelled transition routes through
:func:`assert_legal_transition` so the runtime state-machine
chokepoint is the single source of policy (round-2 reviewer
L155 — phase-7 slice 5a added the ``(PENDING, CANCELLED)``
entry to :data:`LEGAL_TRANSITIONS`).

No-op semantics (round-3 reviewer L544 / Q15): if the
schedule is already in the target status, the helper performs
NO update and appends NO event. The lifecycle tools surface
this case as :meth:`ToolResponse.ok` with an
"already in target state" message.

References:
- ``docs/PHASE_7_PLAN.md`` §3.6 + §5.6a
- ``docs/CONTRACTS_V2_DESIGN.md`` §9
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import Enum
from typing import Callable

from app.v2.enums import EventKind, RunStatus, ScheduleStatus
from app.v2.models.event import Event
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.storage.events import append_event
from app.v2.storage.schedules import (
    ScheduleNotFoundError,
    update_schedule_status,
)


class _LifecycleAction(str, Enum):
    """One of the four schedule-status lifecycle actions.

    Each value maps to a unique
    ``(ScheduleStatus, EventKind)`` pair via
    :data:`_ACTION_TO_STATUS_AND_EVENT`. The mapping is the
    single source of truth — callers cannot mismatch the
    pair (round-2 reviewer L144).
    """

    PAUSE = "pause"
    RESUME = "resume"
    ARCHIVE = "archive"
    REVIVE = "revive"


_ACTION_TO_STATUS_AND_EVENT: dict[
    _LifecycleAction, tuple[ScheduleStatus, EventKind]
] = {
    _LifecycleAction.PAUSE: (
        ScheduleStatus.PAUSED,
        EventKind.SCHEDULE_PAUSED,
    ),
    _LifecycleAction.RESUME: (
        ScheduleStatus.ACTIVE,
        EventKind.SCHEDULE_RESUMED,
    ),
    _LifecycleAction.ARCHIVE: (
        ScheduleStatus.ARCHIVED,
        EventKind.SCHEDULE_ARCHIVED,
    ),
    _LifecycleAction.REVIVE: (
        # Archived → PAUSED gate (round-2 reviewer L442).
        ScheduleStatus.PAUSED,
        EventKind.SCHEDULE_REVIVED,
    ),
}


def update_status_with_event(
    conn: sqlite3.Connection,
    schedule_id: str,
    action: _LifecycleAction,
    *,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
    cancel_pending_runs: bool = False,
) -> None:
    """Atomic status flip + EventLedger append.

    Behaviour:

    - Look up the current schedule status. Missing →
      :class:`ScheduleNotFoundError`.
    - If the current status already matches the target,
      perform NO update and NO event append (Q15 — duplicate
      ``schedule_paused`` rows would look like a second
      transition).
    - Otherwise begin a savepoint:
        1. Append the ``schedule_<action>`` event.
        2. Update the schedule's status.
        3. If ``cancel_pending_runs`` is True (archive
           path), iterate every pending Run for the schedule;
           for each: call
           :func:`assert_legal_transition(PENDING,
           CANCELLED)` (chokepoint pin per L155), update the
           run's status to ``cancelled``, append a
           ``run_cancelled`` event correlated to the
           ``schedule_archived`` event.
        4. Commit the savepoint.

    Caller is responsible for the OUTER transaction (the
    helper uses a SAVEPOINT so it composes with a wrapping
    transaction if any). The implementation does NOT call
    ``conn.commit()`` — the lifecycle tool that owns the
    request is the commit site.
    """
    target_status, event_kind = _ACTION_TO_STATUS_AND_EVENT[action]

    current_row = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    if current_row is None:
        raise ScheduleNotFoundError(
            f"schedules row {schedule_id!r} not found"
        )

    current_status = ScheduleStatus(current_row[0])
    if current_status == target_status:
        # No-op (Q15) — no update, no event.
        return

    savepoint_name = f"lifecycle_{action.value}"
    conn.execute(f"SAVEPOINT {savepoint_name}")
    try:
        # 1. Append the schedule-level event FIRST so it
        # surfaces in the ledger even if the status update
        # later raises (FK / CHECK). Reverse order would
        # leave a moved status without an event.
        schedule_event = Event(
            id=event_id_factory(),
            run_id=None,
            schedule_id=schedule_id,
            ts=clock(),
            kind=event_kind,
            payload={
                "action": action.value,
                "from": current_status.value,
                "to": target_status.value,
            },
        )
        append_event(conn, schedule_event)

        # 2. Update schedule status.
        update_schedule_status(conn, schedule_id, target_status)

        # 3. Cancel pending Runs (archive path only).
        if cancel_pending_runs:
            _cancel_pending_runs(
                conn,
                schedule_id=schedule_id,
                correlates_event_id=schedule_event.id,
                event_id_factory=event_id_factory,
                clock=clock,
            )
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")


def _cancel_pending_runs(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    correlates_event_id: str,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> int:
    """Cancel every pending Run for the schedule + append a
    ``run_cancelled`` event per Run. Returns the count of
    cancelled Runs.

    Routes each transition through
    :func:`assert_legal_transition` so the runtime
    state-machine chokepoint owns the policy (L155 pin).
    """
    pending_rows = conn.execute(
        "SELECT id FROM runs WHERE schedule_id = ? AND status = ?",
        (schedule_id, RunStatus.PENDING.value),
    ).fetchall()
    if not pending_rows:
        return 0

    now = clock()
    cancelled_count = 0
    for (run_id,) in pending_rows:
        # Policy chokepoint pin (L155): cannot bypass even
        # though we know src=PENDING, dst=CANCELLED.
        assert_legal_transition(
            RunStatus.PENDING, RunStatus.CANCELLED
        )
        conn.execute(
            "UPDATE runs SET status = ? WHERE id = ?",
            (RunStatus.CANCELLED.value, run_id),
        )
        run_event = Event(
            id=event_id_factory(),
            run_id=run_id,
            schedule_id=schedule_id,
            ts=now,
            kind=EventKind.RUN_CANCELLED,
            payload={"reason": "schedule_archived"},
            correlates=correlates_event_id,
        )
        append_event(conn, run_event)
        cancelled_count += 1

    return cancelled_count


__all__ = [
    "_ACTION_TO_STATUS_AND_EVENT",
    "_LifecycleAction",
    "update_status_with_event",
]
