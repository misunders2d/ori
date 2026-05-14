"""Compare-and-set state primitive over ``schedule_state``.

Phase 3 slice 7 per ``docs/PHASE_3_PLAN.md`` §5.5 + §7.

Two helpers:

- :func:`get_state` — read a state row by ``(schedule_id, key)``.
- :func:`set_state_cas` — atomic compare-version-and-set.
  Returns ``True`` iff exactly one row was affected; ``False``
  on stale version mismatch. NO internal retry loop — the
  caller owns the retry policy (read fresh version, recompute,
  retry).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2, §4.0.4
- ``docs/PHASE_3_PLAN.md`` §5.5, §7
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.v2.models.state import ScheduleState
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError, encode_json


class StateRunMismatchError(ValueError):
    """Raised by :func:`set_state_cas` when ``written_by_run``
    references a run belonging to a different schedule than
    ``schedule_id``.

    Same audit-truth pattern as
    :class:`app.v2.storage.transactions.EventRunMismatchError`
    + the cross-check inside ``append_event``: both FKs (state →
    schedules, state.written_by_run → runs) pass independently
    while the lineage row falsely attributes a state mutation
    to a run that belongs to a different schedule.
    """


def get_state(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    key: str,
) -> Optional[ScheduleState]:
    """Return the ``ScheduleState`` for ``(schedule_id, key)``,
    or ``None`` when absent.

    Read-only. The returned model's ``value`` is the
    JSON-decoded payload; the runtime decides what shape to
    expect.
    """
    assert_connection_ready(conn)
    row = conn.execute(
        "SELECT schedule_id, key, value_json, version, written_at, "
        "       written_by_run "
        "FROM schedule_state WHERE schedule_id = ? AND key = ?",
        (schedule_id, key),
    ).fetchone()
    if row is None:
        return None
    return ScheduleState(
        schedule_id=row[0],
        key=row[1],
        value=json.loads(row[2]),
        version=row[3],
        written_at=row[4],
        written_by_run=row[5],
    )


def set_state_cas(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    key: str,
    new_value: Any,
    expected_version: int,
    written_by_run: Optional[str],
    now: datetime,
) -> bool:
    """Compare-and-set on ``schedule_state`` rows.

    Semantics (per plan §5.5 + §7):

    - ``expected_version == 0`` — first-time write. The helper
      runs ``INSERT OR IGNORE`` so a concurrent racy first
      write doesn't crash: the loser observes
      ``cursor.rowcount == 0`` and gets ``False`` back, then
      reads fresh state + retries with the current version.
    - ``expected_version >= 1`` — CAS update. The UPDATE filters
      on ``WHERE schedule_id=? AND key=? AND version=?``. Match
      → row bumped (version += 1, value_json + written_at +
      written_by_run replaced). No match → ``False`` returned;
      caller retries.

    No internal retry loop — the caller knows the right policy
    for their use case (retry now, fall through, abort). Phase 3
    is the primitive; phase 4 wires the policy.

    Args:
        conn: caller-owned migrated connection.
        schedule_id: composite-PK left half.
        key: composite-PK right half.
        new_value: any JSON-serialisable Python value (dict /
            list / scalar). datetime values inside the structure
            are rejected if naive (the serialiser walks them).
        expected_version: ``0`` to seed; ``>= 1`` to CAS-update.
            Negative values raise ``ValueError``.
        written_by_run: the run id writing this state, or
            ``None`` for author-time seeds. FK enforced by the
            DDL — a ghost run id raises ``IntegrityError`` at
            INSERT/UPDATE time.
        now: tz-aware datetime; naive raises
            ``NaiveDatetimeError``. Stored as UTC-normalised
            ISO 8601 (same rule as the rest of the storage
            layer).

    Returns:
        ``True`` when exactly one row was inserted / updated;
        ``False`` on stale version OR concurrent first-write
        collision.

    Raises:
        ValueError: ``expected_version < 0``.
        NaiveDatetimeError: ``now`` was naive.
        ConnectionNotReady: bad connection state.
        sqlite3.IntegrityError: ``written_by_run`` references
            a non-existent run.
    """
    assert_connection_ready(conn)

    if expected_version < 0:
        raise ValueError(
            f"expected_version must be >= 0; got {expected_version}. "
            "Use 0 for the first write, >= 1 for subsequent CAS "
            "updates."
        )
    if now.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in now: {now!r} — set_state_cas "
            "requires a tz-aware timestamp so written_at is "
            "comparable across rows."
        )

    # Cross-check that ``written_by_run`` (when set) names a run
    # belonging to ``schedule_id``. Without this, both FKs would
    # pass while the lineage row would falsely attribute a
    # state mutation to a run from a different schedule. The
    # SELECT happens BEFORE any INSERT/UPDATE so a mismatch
    # raises without touching the row. Missing run continues to
    # route through the FK constraint (IntegrityError).
    if written_by_run is not None:
        row = conn.execute(
            "SELECT schedule_id FROM runs WHERE id = ?",
            (written_by_run,),
        ).fetchone()
        if row is not None and row[0] != schedule_id:
            raise StateRunMismatchError(
                f"written_by_run={written_by_run!r} belongs to "
                f"schedule {row[0]!r}, but the state row is "
                f"for schedule {schedule_id!r}. Both FKs would "
                "pass yet the lineage attribution would be wrong "
                "— refusing the write."
            )

    now_iso = now.astimezone(timezone.utc).isoformat()
    value_json = encode_json(new_value)

    if expected_version == 0:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO schedule_state "
            "(schedule_id, key, value_json, version, written_at, "
            " written_by_run) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (
                schedule_id,
                key,
                value_json,
                now_iso,
                written_by_run,
            ),
        )
        return cursor.rowcount == 1

    cursor = conn.execute(
        "UPDATE schedule_state "
        "SET value_json = ?, "
        "    version = version + 1, "
        "    written_at = ?, "
        "    written_by_run = ? "
        "WHERE schedule_id = ? AND key = ? AND version = ?",
        (
            value_json,
            now_iso,
            written_by_run,
            schedule_id,
            key,
            expected_version,
        ),
    )
    return cursor.rowcount == 1


__all__ = [
    "StateRunMismatchError",
    "get_state",
    "set_state_cas",
]
