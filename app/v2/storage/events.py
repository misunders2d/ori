"""Append-only ledger over the ``events`` table.

Phase 3 slice 6 per ``docs/PHASE_3_PLAN.md`` §5.4.

Surface is intentionally write-once + read:

- :func:`append_event` — insert a single Event row. No
  ``update_*`` / ``delete_*`` exists.
- :func:`list_events_for_run` — chronological per-run history.
- :func:`list_events_for_schedule` — recent-first per-schedule
  log; optional ``kind`` filter; ``limit`` defaults to 200.
- :func:`get_last_emit_succeeded` — ledger-side dedup lookup
  by idempotency key. Used by the future emit adapters to
  short-circuit a retry whose previous attempt already
  succeeded.

The same-TX-as-transition rule (design §4.0.4 row 4) is
satisfied by :func:`app.v2.storage.transactions.update_run_status_and_append_event`,
not by this module. ``append_event`` here is for standalone
events (schedule-level lifecycle, source resolution, etc.) and
for callers already inside a transaction set up elsewhere.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2, §4.0.4, §6.4
- ``docs/PHASE_3_PLAN.md`` §5.4, §8
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.v2.enums import EventKind
from app.v2.models.event import Event
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError, encode_json
from app.v2.storage.transactions import EventRunMismatchError


_EVENT_COLUMNS = (
    "id",
    "run_id",
    "schedule_id",
    "ts",
    "kind",
    "payload_json",
    "correlates",
)
_SELECT_SQL = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM events"


def _event_ts_to_utc_iso(ts: datetime, *, field: str = "event.ts") -> str:
    """UTC-normalise an event timestamp to ISO 8601.

    Carries the same hazard pinned in
    ``app.v2.storage.runs._to_iso_or_none``: ISO strings are
    compared lexically in SQL, so storing mixed offsets would
    break ``ORDER BY ts`` and the ``idx_events_schedule_ts``
    index. Normalising to UTC at the storage boundary keeps the
    compare sound. Naive datetimes raise loud.
    """
    if ts.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in {field}: {ts!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )
    return ts.astimezone(timezone.utc).isoformat()


def _row_to_event(row: tuple) -> Event:
    (
        id_,
        run_id,
        schedule_id,
        ts,
        kind_raw,
        payload_raw,
        correlates,
    ) = row
    return Event(
        id=id_,
        run_id=run_id,
        schedule_id=schedule_id,
        ts=ts,
        kind=EventKind(kind_raw),
        payload=json.loads(payload_raw),
        correlates=correlates,
    )


def append_event(conn: sqlite3.Connection, event: Event) -> str:
    """Insert a single Event row. Returns ``event.id``.

    ``event.ts`` MUST be timezone-aware; naive datetimes raise
    ``NaiveDatetimeError`` and the helper writes nothing.
    Stored ``ts`` is always normalised to ``+00:00`` so the
    composite index over ``(schedule_id, ts)`` orders
    chronologically regardless of caller offset.

    **Run / schedule consistency** (matches the same guard in
    :func:`app.v2.storage.transactions.update_run_status_and_append_event`):
    when ``event.run_id`` is set, the helper looks up the
    referenced run's ``schedule_id`` and refuses to write if it
    does not equal ``event.schedule_id``. Without this check
    both FK constraints could pass (run exists, schedule
    exists) while the ledger row falsely attributes the
    event to a different schedule. ``event.run_id is None``
    skips the cross-check entirely (schedule-level events
    have no run to reconcile against).

    No update / delete helper exists in this module. Phase-3
    contract: events are append-only at the surface; the
    schema does not enforce it (no triggers) but the API does.

    Raises:
        NaiveDatetimeError: ``event.ts`` was naive.
        EventRunMismatchError: ``event.run_id`` is set and the
            referenced run's ``schedule_id`` does not equal
            ``event.schedule_id``.
        sqlite3.IntegrityError: ``event.run_id`` references a
            run row that does not exist (FK enforced at INSERT).
        ConnectionNotReady: bad connection state.
    """
    assert_connection_ready(conn)
    ts_iso = _event_ts_to_utc_iso(event.ts, field="event.ts")

    if event.run_id is not None:
        row = conn.execute(
            "SELECT schedule_id FROM runs WHERE id = ?",
            (event.run_id,),
        ).fetchone()
        # Missing run row → fall through to the INSERT and let
        # the FK constraint raise IntegrityError. The reviewer
        # accepted either path; FK gives a uniform error type
        # for "run does not exist".
        if row is not None and row[0] != event.schedule_id:
            raise EventRunMismatchError(
                f"event.schedule_id={event.schedule_id!r} does "
                f"not match run.schedule_id={row[0]!r} for "
                f"event.run_id={event.run_id!r}. Both FK "
                "constraints would pass, but the ledger row "
                "would falsely attribute this event to a "
                "different schedule — refusing the write."
            )

    conn.execute(
        f"INSERT INTO events ({', '.join(_EVENT_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(_EVENT_COLUMNS))})",
        (
            event.id,
            event.run_id,
            event.schedule_id,
            ts_iso,
            event.kind.value,
            encode_json(event.payload),
            event.correlates,
        ),
    )
    return event.id


def list_events_for_run(
    conn: sqlite3.Connection,
    run_id: str,
) -> list[Event]:
    """Return every event linked to ``run_id``, oldest first.

    Chronological order is the right default for per-run audit
    — readers replay the lifecycle in the order it happened.
    Empty list when no rows match (e.g. a schedule-level event
    has ``run_id IS NULL`` and won't appear here).
    """
    assert_connection_ready(conn)
    rows = conn.execute(
        f"{_SELECT_SQL} WHERE run_id = ? ORDER BY ts ASC",
        (run_id,),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def list_events_for_schedule(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    kind: Optional[EventKind] = None,
    limit: int = 200,
) -> list[Event]:
    """Return events for ``schedule_id``, most recent first.

    ``kind``, when set, filters to that exact event kind.
    ``limit`` defaults to 200 (caller can override). ``limit``
    MUST be >= 1; ValueError otherwise (same guard as
    ``runs.list_pending_due``).

    Order is DESC because the typical caller is a diagnostic
    /admin view — "show me the latest 50 events for this
    schedule". A future replay-style tool that needs
    chronological order can call this twice / reverse the
    result.
    """
    assert_connection_ready(conn)
    if limit < 1:
        raise ValueError(
            f"limit must be >= 1; got {limit}. SQLite treats "
            "LIMIT -1 as 'no limit' — refusing to forward a "
            "potentially-unbounded query."
        )

    if kind is None:
        sql = (
            f"{_SELECT_SQL} WHERE schedule_id = ? "
            "ORDER BY ts DESC LIMIT ?"
        )
        params: tuple = (schedule_id, limit)
    else:
        sql = (
            f"{_SELECT_SQL} WHERE schedule_id = ? AND kind = ? "
            "ORDER BY ts DESC LIMIT ?"
        )
        params = (schedule_id, kind.value, limit)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(row) for row in rows]


def get_last_emit_succeeded(
    conn: sqlite3.Connection,
    idempotency_key: str,
) -> Optional[Event]:
    """Return the most recent ``emit_succeeded`` event whose
    payload carries the given ``idempotency_key``, or ``None``.

    The runtime emit adapter writes the idempotency key under
    the literal payload field ``"idempotency_key"`` (per
    convention documented in
    ``docs/CONTRACTS_V2_DESIGN.md`` §6.4 + plan §8.4). This
    helper queries via ``json_extract`` against that path. The
    schema does not declare a generated column for the key, so
    this is an O(n) scan for now — acceptable while the ledger
    is small; phase-4 may add an indexed path if hot-path
    profiling shows the need.

    The kind filter ``= 'emit_succeeded'`` is the load-bearing
    correctness check: an ``emit_failed`` event with the same
    idempotency key does NOT count as a dedup hit; the worker
    must retry.
    """
    assert_connection_ready(conn)
    row = conn.execute(
        f"{_SELECT_SQL} "
        "WHERE kind = ? "
        "AND json_extract(payload_json, '$.idempotency_key') = ? "
        "ORDER BY ts DESC LIMIT 1",
        (EventKind.EMIT_SUCCEEDED.value, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    return _row_to_event(row)


__all__ = [
    "append_event",
    "list_events_for_run",
    "list_events_for_schedule",
    "get_last_emit_succeeded",
]
