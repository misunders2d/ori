"""Single-flight claim primitive for the v2 runtime.

Phase 4 slice 2 per ``docs/PHASE_4_PLAN.md`` §4.

Wraps the design §6.1 UPDATE plus the round-1 schedule-status
predicate. One atomic SQL statement decides whether THIS
worker owns the run:

1. The targeted row is still ``pending``.
2. No OTHER run on the same schedule is currently
   ``claimed`` or ``running`` (single-flight; the
   ``r2.id != runs.id`` clause excludes the target row
   itself so the predicate is independent of UPDATE-WHERE
   evaluation ordering in any SQLite version).
3. The owning schedule is in ``active`` status (so pending
   rows that pre-dated a pause / archive cannot be claimed).

The state-machine policy (``PENDING → CLAIMED``) is asserted
via ``assert_legal_transition`` before any SQL is run, so
future tightening of ``LEGAL_TRANSITIONS`` automatically
disables this primitive rather than silently letting it write
an illegal transition.

If all three hold, the row flips to ``claimed`` with
``claimed_by`` / ``claimed_at`` populated AND a paired
``run_claimed`` Event lands in the same TX. Otherwise the
helper returns ``False`` — no row changed, no event written.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §6.1, §4.0.4
- ``docs/PHASE_4_PLAN.md`` §4
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.v2.enums import EventKind, RunStatus, ScheduleStatus
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError, encode_json
from app.v2.storage.transactions import transaction


def claim_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    claimed_by: str,
    now: datetime,
    event_id: str,
) -> bool:
    """Atomically claim a pending run.

    Returns ``True`` when this caller owns the run after the
    call; ``False`` when any of the predicates failed (lost
    race, status mismatch, paused/archived schedule, etc.).
    Atomicity-on-failure: if the paired event INSERT raises,
    the claim UPDATE rolls back.

    Args:
        conn: caller-owned migrated SQLite connection.
        run_id: PK of the run to claim.
        claimed_by: worker identifier (any string).
        now: tz-aware datetime; naive raises
            ``NaiveDatetimeError``. UTC-normalised on the way
            to SQL so the stored ``claimed_at`` matches the
            convention from the storage layer.
        event_id: caller-supplied id for the ``run_claimed``
            event row. Injected so tests can be deterministic
            (production wires ``uuid.uuid4().hex`` in
            ``runtime/_defaults.py``, NOT in this module).

    Raises:
        NaiveDatetimeError: ``now`` was naive.
        ConnectionNotReady: bad connection state.
        sqlite3.IntegrityError: ``event_id`` collides with an
            existing event row (rolls back the claim UPDATE).
    """
    assert_connection_ready(conn)
    # State-machine policy gate. Asserts that PENDING → CLAIMED
    # is still a legal phase-4 transition. Hardcoded endpoints
    # here because the SQL predicate also hardcodes them; if
    # LEGAL_TRANSITIONS is ever narrowed to remove this pair the
    # claim primitive must be reviewed (not silently skipped).
    assert_legal_transition(RunStatus.PENDING, RunStatus.CLAIMED)
    if now.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in now: {now!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )
    now_iso = now.astimezone(timezone.utc).isoformat()

    update_sql = (
        "UPDATE runs SET status = ?, claimed_by = ?, claimed_at = ? "
        "WHERE id = ? "
        "AND status = ? "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM runs r2 "
        "    WHERE r2.schedule_id = runs.schedule_id "
        "    AND r2.id != runs.id "
        "    AND r2.status IN (?, ?)"
        ") "
        "AND EXISTS ("
        "    SELECT 1 FROM schedules s "
        "    WHERE s.id = runs.schedule_id "
        "    AND s.status = ?"
        ")"
    )
    update_params = (
        RunStatus.CLAIMED.value,
        claimed_by,
        now_iso,
        run_id,
        RunStatus.PENDING.value,
        RunStatus.CLAIMED.value,
        RunStatus.RUNNING.value,
        ScheduleStatus.ACTIVE.value,
    )

    with transaction(conn):
        cursor = conn.execute(update_sql, update_params)
        if cursor.rowcount == 0:
            # Lost the race, status guard fired, single-flight
            # fired, or schedule paused/archived. None of these
            # are errors — return False so the worker tries
            # another run next tick.
            return False

        # Fetch the schedule_id so the paired event row attributes
        # to the same schedule the run belongs to. (We don't pass
        # schedule_id as an argument — the caller may not know
        # it; the run row is authoritative.)
        row = conn.execute(
            "SELECT schedule_id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        schedule_id = row[0]

        conn.execute(
            "INSERT INTO events "
            "(id, run_id, schedule_id, ts, kind, payload_json, correlates) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                run_id,
                schedule_id,
                now_iso,
                EventKind.RUN_CLAIMED.value,
                encode_json({"claimed_by": claimed_by}),
                None,
            ),
        )
    return True


__all__ = ["claim_run"]
