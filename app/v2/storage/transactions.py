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


class EventRunMismatchError(ValueError):
    """Raised when the helper's ``event`` argument refers to a
    different run / schedule than the row being updated.

    The atomic helper's audit-truth promise depends on the
    event row matching the run row it was emitted alongside.
    Without this check the ledger could record "run B succeeded"
    while run A is the one whose status actually changed —
    exactly the corruption the same-TX-as-transition invariant
    is supposed to prevent.
    """


class UnknownExtraColumnError(ValueError):
    """Raised when ``extra_columns`` contains a key that is not
    in the phase-3 allowlist.

    The helper interpolates the key directly into the UPDATE
    statement, so an unrestricted key set is a SQL-injection
    surface AND a way to silently mutate columns the helper
    isn't supposed to touch (``status``, ``attempt``,
    ``root_run_id``, ``schedule_id``, future claim columns).
    Phase 3 allows only the three neutral run-lifecycle columns
    every step of the future state machine will need to set;
    ``claimed_by`` / ``claimed_at`` stay out until phase 4
    introduces claim semantics.
    """


# Columns the helper is allowed to set via ``extra_columns``.
# Each entry is the EXACT SQL identifier — the helper does NOT
# accept partial / templated identifiers. New entries land here
# only when a corresponding storage need shows up in a later
# slice / phase plan.
_ALLOWED_EXTRA_COLUMNS: frozenset[str] = frozenset(
    {
        "started_at",
        "completed_at",
        "error",
    }
)


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

    The ``runs`` UPDATE is unpredicated on source status — no
    single-flight check, no claim-ownership predicate. Phase 4's
    state machine owns the legal-transitions policy; phase 3
    owns only the data-plane atomicity guarantee.

    The helper DOES enforce two consistency checks that have
    nothing to do with the state machine and everything to do
    with audit-ledger truth:

    - ``event.run_id`` MUST equal ``run_id``. The paired event
      describes the run being updated; if those disagree the
      ledger would claim a different run changed.
    - ``event.schedule_id`` MUST equal the updated run's
      ``schedule_id`` (fetched inside the same TX). Without this
      a malformed caller could attribute a run-A event to
      schedule-B.

    Args:
        conn: caller-owned SQLite connection on a WAL DB with
            ``v001_initial`` applied (validated via
            :func:`assert_connection_ready`).
        run_id: PK of the run row to mutate.
            ``RunNotFoundError`` is raised when no row matches.
        new_status: new ``runs.status`` value.
        event: Pydantic ``Event`` describing the matching ledger
            row. ``event.ts`` MUST be timezone-aware; naive
            datetimes raise ``NaiveDatetimeError``.
            ``event.run_id`` MUST equal ``run_id``;
            ``event.schedule_id`` MUST equal the updated run's
            schedule_id. Mismatch raises
            ``EventRunMismatchError``.
        extra_columns: optional mapping of additional ``runs``
            columns to set. ONLY keys in the phase-3 allowlist
            (``started_at``, ``completed_at``, ``error``) are
            accepted; anything else raises
            ``UnknownExtraColumnError``. The allowlist is a
            hard SQL-injection guard: keys go straight into the
            UPDATE identifier list, so they must come from a
            fixed set the storage layer controls. Datetime
            values are converted to ISO 8601 strings and
            rejected if naive.

    Raises:
        RunNotFoundError: when no row in ``runs`` has
            ``id = run_id``.
        EventRunMismatchError: when ``event.run_id`` !=
            ``run_id`` OR ``event.schedule_id`` != the updated
            run's ``schedule_id``.
        UnknownExtraColumnError: when ``extra_columns`` contains
            a key outside the phase-3 allowlist.
        NaiveDatetimeError: when ``event.ts`` or any datetime
            in ``extra_columns`` is naive.
        ConnectionNotReady: when the connection is missing WAL
            / foreign_keys / v001 migration.
    """
    assert_connection_ready(conn)

    if event.ts.tzinfo is None:
        raise NaiveDatetimeError(
            f"event.ts is naive: {event.ts!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )

    if event.run_id != run_id:
        raise EventRunMismatchError(
            f"event.run_id={event.run_id!r} does not match "
            f"updated run_id={run_id!r}. The paired event must "
            "describe the run being updated."
        )

    extras = dict(extra_columns or {})
    unknown = set(extras) - _ALLOWED_EXTRA_COLUMNS
    if unknown:
        raise UnknownExtraColumnError(
            f"extra_columns contains unknown / unsafe keys: "
            f"{sorted(unknown)}. Phase 3 allows only "
            f"{sorted(_ALLOWED_EXTRA_COLUMNS)}; everything else "
            "(``status``, ``attempt``, ``root_run_id``, "
            "``schedule_id``, ``claimed_by`` / ``claimed_at``, "
            "etc.) is rejected — the helper interpolates these "
            "keys directly into SQL and must work from a fixed "
            "allowlist, not arbitrary caller input."
        )

    coerced_extras = {k: _serialize_extra_value(v) for k, v in extras.items()}

    # Identifiers are pulled from the allowlist (not formatted
    # from raw caller input) — this is the load-bearing guard
    # against arbitrary-column writes.
    extra_keys = sorted(coerced_extras)
    set_columns = ["status = ?"] + [f"{k} = ?" for k in extra_keys]
    update_values: list[Any] = [new_status.value] + [
        coerced_extras[k] for k in extra_keys
    ]
    update_values.append(run_id)
    update_sql = f"UPDATE runs SET {', '.join(set_columns)} WHERE id = ?"

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
        # Schedule-id check happens INSIDE the TX so we read the
        # row the UPDATE will mutate — no TOCTOU window even
        # under concurrent writers (WAL serialises writers).
        row = conn.execute(
            "SELECT schedule_id FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(
                f"runs row {run_id!r} not found — refusing to "
                "append a paired event."
            )
        actual_schedule_id = row[0]
        if event.schedule_id != actual_schedule_id:
            raise EventRunMismatchError(
                f"event.schedule_id={event.schedule_id!r} does "
                f"not match run.schedule_id="
                f"{actual_schedule_id!r}. The ledger row must "
                "attribute the event to the same schedule the "
                "run belongs to."
            )

        cursor = conn.execute(update_sql, update_values)
        if cursor.rowcount == 0:
            # Defensive: should be unreachable inside the same
            # WAL TX after the SELECT confirmed the row exists,
            # but the explicit check keeps the helper loud if
            # the schema ever drifts.
            raise RunNotFoundError(
                f"runs row {run_id!r} vanished between SELECT "
                "and UPDATE — refusing to append a paired event."
            )
        conn.execute(insert_sql, insert_values)


__all__ = [
    "EventRunMismatchError",
    "RunNotFoundError",
    "UnknownExtraColumnError",
    "transaction",
    "update_run_status_and_append_event",
]
