"""V2 scheduler — schedule lifecycle authoring tools.

Phase 7 slice 5b per ``docs/PHASE_7_PLAN.md`` §3.6 + §5.6b.

Four tools that flip a schedule's status:

- :func:`schedule_pause` — active → paused.
- :func:`schedule_resume` — paused → active. Refuses when
  current is archived (round-2 reviewer L442 gate — admin
  re-approves via :func:`schedule_revive` to PAUSED first).
- :func:`schedule_archive` — any → archived. Cancels every
  pending Run in the same transaction (round-2 L598).
- :func:`schedule_revive` — archived → **PAUSED** (round-2
  L442 — not directly active).

All four funnel through :func:`update_status_with_event`
so the schedule-status flip and the matching EventLedger
event land in a single transaction. The helper also owns
the no-op shortcut for already-in-target-status calls
(round-3 L544 / Q15 — no status update, no event).

Phase 7 does NOT call the phase-5
:mod:`app.v2.runtime.lifecycle` hooks; the APScheduler
binding is not mounted in phase 7. Phase-9 cutover wires
the hook calls alongside these tools.

References:
- ``docs/PHASE_7_PLAN.md`` §3.6 + §5.6b
- ``docs/CONTRACTS_V2_DESIGN.md`` §11.4
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from app.v2.authoring.lifecycle_helper import (
    _LifecycleAction,
    update_status_with_event,
)
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.enums import ScheduleStatus
from app.v2.storage.schedules import ScheduleNotFoundError


def _not_found(schedule_id: str) -> ToolResponse:
    return ToolResponse.not_found(
        message=f"schedule {schedule_id!r} not found"
    )


def _current_status(
    conn: sqlite3.Connection, schedule_id: str
) -> ScheduleStatus | None:
    row = conn.execute(
        "SELECT status FROM schedules WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    if row is None:
        return None
    return ScheduleStatus(row[0])


# ---------------------------------------------------------------------------
# schedule_pause
# ---------------------------------------------------------------------------


async def schedule_pause(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Flip a schedule's status to ``paused``.

    No-op (round-3 L544 / Q15) when current status is already
    ``paused``: returns :meth:`ToolResponse.ok` with an
    ``already paused`` hint.
    """
    current = _current_status(conn, schedule_id)
    if current is None:
        return _not_found(schedule_id)

    already_target = current == ScheduleStatus.PAUSED
    try:
        update_status_with_event(
            conn,
            schedule_id,
            _LifecycleAction.PAUSE,
            event_id_factory=event_id_factory,
            clock=clock,
        )
    except ScheduleNotFoundError:
        return _not_found(schedule_id)
    conn.commit()

    if already_target:
        return ToolResponse.ok(
            schedule_id=schedule_id,
            message=f"schedule {schedule_id!r} already paused",
        )
    return ToolResponse.ok(schedule_id=schedule_id)


# ---------------------------------------------------------------------------
# schedule_resume
# ---------------------------------------------------------------------------


async def schedule_resume(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Flip a schedule's status to ``active``.

    Refuses (round-2 L442) when current status is
    ``archived``: the design §11.4 admin re-approval gate
    requires :func:`schedule_revive` to PAUSED first.

    No-op when current is already ``active``.
    """
    current = _current_status(conn, schedule_id)
    if current is None:
        return _not_found(schedule_id)

    if current == ScheduleStatus.ARCHIVED:
        return _validation_failed_single(
            code="resume_from_archived_blocked",
            path="status",
            message=(
                f"schedule {schedule_id!r} is archived; call "
                "schedule_revive first to move it to paused, "
                "then schedule_resume to active (design §11.4)"
            ),
        )

    already_target = current == ScheduleStatus.ACTIVE
    try:
        update_status_with_event(
            conn,
            schedule_id,
            _LifecycleAction.RESUME,
            event_id_factory=event_id_factory,
            clock=clock,
        )
    except ScheduleNotFoundError:
        return _not_found(schedule_id)
    conn.commit()

    if already_target:
        return ToolResponse.ok(
            schedule_id=schedule_id,
            message=f"schedule {schedule_id!r} already active",
        )
    return ToolResponse.ok(schedule_id=schedule_id)


# ---------------------------------------------------------------------------
# schedule_archive
# ---------------------------------------------------------------------------


async def schedule_archive(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Flip a schedule's status to ``archived`` AND cancel
    every pending Run for it in the same transaction
    (round-2 L598).

    The response's ``message`` reports the cancelled-Run
    count so the LLM can echo it back to the user.

    No-op when current is already ``archived``: returns
    ``ok`` with ``already archived`` hint.
    """
    current = _current_status(conn, schedule_id)
    if current is None:
        return _not_found(schedule_id)

    already_target = current == ScheduleStatus.ARCHIVED
    try:
        cancelled = update_status_with_event(
            conn,
            schedule_id,
            _LifecycleAction.ARCHIVE,
            event_id_factory=event_id_factory,
            clock=clock,
            cancel_pending_runs=True,
        )
    except ScheduleNotFoundError:
        return _not_found(schedule_id)
    conn.commit()

    if already_target:
        return ToolResponse.ok(
            schedule_id=schedule_id,
            message=f"schedule {schedule_id!r} already archived",
        )
    return ToolResponse.ok(
        schedule_id=schedule_id,
        message=(
            f"archived; cancelled {cancelled} pending run(s)"
        ),
    )


# ---------------------------------------------------------------------------
# schedule_revive
# ---------------------------------------------------------------------------


async def schedule_revive(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Flip a schedule's status from ``archived`` to
    **PAUSED** (round-2 L442 gate). Admin re-approves via
    :func:`schedule_resume` afterwards to move to ``active``.

    No-op when current is already ``paused`` (the revive
    action's target status); returns ``ok`` with an
    ``already paused`` hint.
    """
    current = _current_status(conn, schedule_id)
    if current is None:
        return _not_found(schedule_id)

    already_target = current == ScheduleStatus.PAUSED
    try:
        update_status_with_event(
            conn,
            schedule_id,
            _LifecycleAction.REVIVE,
            event_id_factory=event_id_factory,
            clock=clock,
        )
    except ScheduleNotFoundError:
        return _not_found(schedule_id)
    conn.commit()

    if already_target:
        return ToolResponse.ok(
            schedule_id=schedule_id,
            message=f"schedule {schedule_id!r} already paused",
        )
    return ToolResponse.ok(schedule_id=schedule_id)


__all__ = [
    "schedule_archive",
    "schedule_pause",
    "schedule_resume",
    "schedule_revive",
]
