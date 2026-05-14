"""Explicit-transaction primitives for the v2 storage layer.

Two helpers:

- :func:`transaction` — a context manager that wraps a block of
  storage calls in ``BEGIN ... COMMIT``. Any exception rolls
  back and re-raises. Owns the transaction; caller MUST NOT
  already be inside one. Nested transactions are not supported
  in phase 3 (SQLite doesn't have real nesting, and
  ``SAVEPOINT`` emulation invites bugs that would surface at
  the worst possible time).

- :func:`update_run_status_and_append_event` — a neutral storage
  primitive that bundles a Run row update + the matching Event
  row insert into one atomic transaction. The design's "ledger
  transactionality" invariant (§4.0.4 row 4) requires both
  writes land together. This helper is DELIBERATELY policy-free:
  it does an unpredicated ``UPDATE runs WHERE id = ?`` — no
  source-status guard, no single-flight predicate, no claim
  ownership check. Phase-4 state-machine logic decides which
  transitions are legal; phase 3 owns only the atomicity
  guarantee. Raises :class:`RunNotFoundError` when the UPDATE
  affects 0 rows.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.4 (ledger transactionality)
- ``docs/PHASE_3_PLAN.md`` §4 (transaction API)
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from app.v2.enums import RunStatus
from app.v2.models.event import Event
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError, encode_json


class RunNotFoundError(LookupError):
    """Raised when :func:`update_run_status_and_append_event`
    targets a run id that has no row in the ``runs`` table.

    ``LookupError`` subclass so callers can catch the generic
    "not found" case with the standard exception type while
    still distinguishing it from other lookup-style errors via
    the dedicated class.
    """


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block inside an explicit ``BEGIN ... COMMIT``.

    On a clean exit the helper issues ``COMMIT``. On any
    exception (including ``BaseException`` like
    ``KeyboardInterrupt``) it issues ``ROLLBACK`` and re-raises.

    The helper owns the transaction. The caller MUST NOT already
    be inside an active transaction when invoking this — SQLite
    will refuse the inner ``BEGIN`` with "cannot start a
    transaction within a transaction". Tests pin this so the
    surface-level rule stays loud.

    ``isolation_level`` is set to ``None`` (autocommit) so the
    explicit ``BEGIN`` works regardless of Python's legacy
    sqlite3 implicit-transaction behavior. The migration runner
    already does this on the connection it runs against; we
    re-set it only when needed because Python's sqlite3 module
    has a sharp edge — assigning ``isolation_level`` (even the
    same None value) silently COMMITs any active transaction.
    Guarding against the no-op assignment is what keeps a
    nested ``transaction(conn)`` call surface the
    ``cannot start a transaction within a transaction`` error
    SQLite emits, rather than silently closing the parent TX.
    """
    if conn.isolation_level is not None:
        conn.isolation_level = None
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _serialize_extra_value(value: Any) -> Any:
    """Coerce an ``extra_columns`` value to its SQLite-storable
    form, rejecting naive datetimes.

    ``datetime`` values become ISO 8601 strings; everything else
    is returned as-is. The serialization layer already enforces
    timezone-aware datetimes elsewhere — this helper extends the
    same rule to the ``extra_columns`` channel so a caller can't
    smuggle a naive ``started_at`` past the storage boundary.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NaiveDatetimeError(
                "naive datetime in extra_columns: "
                f"{value!r} — attach tzinfo (typically "
                "datetime.timezone.utc) before passing."
            )
        return value.isoformat()
    return value


def update_run_status_and_append_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    new_status: RunStatus,
    event: Event,
    extra_columns: Optional[dict[str, Any]] = None,
) -> None:
    """Atomically update a ``runs`` row and insert the matching
    ``events`` row inside one ``BEGIN ... COMMIT``.

    The ``runs`` UPDATE is unpredicated — it sets ``status`` (and
    any keys in ``extra_columns``) ``WHERE id = ?`` with no
    source-status check, no single-flight predicate. The caller
    is responsible for any prior consistency check; phase 3 only
    owns the data-plane atomicity guarantee. Phase 4's state
    machine introduces the legal-transitions policy.

    Args:
        conn: caller-owned SQLite connection on a WAL DB with
            ``v001_initial`` applied (validated via
            :func:`assert_connection_ready`).
        run_id: PK of the run row to mutate. ``RunNotFoundError``
            is raised when no row matches.
        new_status: new ``runs.status`` value.
        event: Pydantic ``Event`` describing the matching ledger
            row. ``event.ts`` MUST be timezone-aware; naive
            datetimes raise ``NaiveDatetimeError``.
        extra_columns: optional mapping of additional ``runs``
            columns to set (e.g. ``started_at``,
            ``completed_at``, ``error``). Datetime values are
            converted to ISO 8601 strings and rejected if naive.

    Raises:
        RunNotFoundError: when no row in ``runs`` has
            ``id = run_id``.
        NaiveDatetimeError: when ``event.ts`` or any value in
            ``extra_columns`` is a naive datetime.
        ConnectionNotReady: when the connection is missing WAL /
            foreign_keys / v001 migration.
    """
    assert_connection_ready(conn)

    if event.ts.tzinfo is None:
        raise NaiveDatetimeError(
            f"event.ts is naive: {event.ts!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )

    extras = dict(extra_columns or {})
    coerced_extras = {k: _serialize_extra_value(v) for k, v in extras.items()}

    set_columns = ["status = ?"] + [f"{k} = ?" for k in coerced_extras]
    update_values: list[Any] = [new_status.value] + list(coerced_extras.values())
    update_values.append(run_id)
    update_sql = (
        f"UPDATE runs SET {', '.join(set_columns)} WHERE id = ?"
    )

    insert_sql = (
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, payload_json, correlates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    insert_values = (
        event.id,
        event.run_id,
        event.schedule_id,
        event.ts.isoformat(),
        event.kind.value,
        encode_json(event.payload),
        event.correlates,
    )

    with transaction(conn):
        cursor = conn.execute(update_sql, update_values)
        if cursor.rowcount == 0:
            raise RunNotFoundError(
                f"runs row {run_id!r} not found — UPDATE affected "
                "0 rows; refusing to append a paired event."
            )
        conn.execute(insert_sql, insert_values)


__all__ = [
    "RunNotFoundError",
    "transaction",
    "update_run_status_and_append_event",
]
