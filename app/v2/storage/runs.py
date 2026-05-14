"""CRUD over the ``runs`` table.

Phase 3 slice 5 per ``docs/PHASE_3_PLAN.md`` §5.3.

Surface is deliberately neutral — no claim semantics, no
single-flight predicate, no source-status guard. Phase 4
introduces the worker that owns claim ownership; phase 3 just
exposes the data plane:

- :func:`insert_run` — write a Pydantic ``Run`` to the table.
- :func:`get_run` — read a row back into a Pydantic ``Run``.
- :func:`list_pending_due` — READ-ONLY query of pending runs
  with ``due_at <= now``, ordered by ``due_at``. Never claims.
- :func:`list_runs_in_chain` — retry chain via
  ``WHERE root_run_id = ?``, ordered by ``attempt``.
- :func:`mark_run_status` — unpredicated ``UPDATE runs SET
  status = ? WHERE id = ?``. No source-status check.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2 + §4.0.4
- ``docs/PHASE_3_PLAN.md`` §5.3
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Optional

from app.v2.enums import RunStatus
from app.v2.models.run import Run
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError
from app.v2.storage.transactions import (
    RunNotFoundError,
    UnknownExtraColumnError,
)


# Allowlist for ``mark_run_status``'s ``**extra`` kwargs. Same
# set as ``transactions._ALLOWED_EXTRA_COLUMNS`` — both surfaces
# write the same table with the same neutral lifecycle columns.
# A drift guard in the test suite asserts the two sets stay
# equal. Phase-4 claim columns (``claimed_by`` / ``claimed_at``)
# stay out.
ALLOWED_EXTRA_RUN_COLUMNS: frozenset[str] = frozenset(
    {
        "started_at",
        "completed_at",
        "error",
    }
)


_RUN_COLUMNS = (
    "id",
    "schedule_id",
    "execution_plan_hash",
    "fire_reason",
    "due_at",
    "status",
    "attempt",
    "root_run_id",
    "parent_run_id",
    "claimed_by",
    "claimed_at",
    "started_at",
    "completed_at",
    "error",
)
_SELECT_SQL = f"SELECT {', '.join(_RUN_COLUMNS)} FROM runs"


def _to_iso_or_none(dt: Optional[datetime], *, field: str) -> Optional[str]:
    """Convert an optional ``datetime`` to ISO 8601, rejecting
    naive values loud.

    ``field`` appears in the error message so the caller knows
    which field carried the naive value.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in {field}: {dt!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )
    return dt.isoformat()


def _coerce_extra_value(value: Any, *, field: str) -> Any:
    """Same naive-rejection / ISO-coercion as
    :func:`_to_iso_or_none` but lifts the field name into the
    error path for ``mark_run_status``'s extras."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NaiveDatetimeError(
                f"naive datetime in extra_columns.{field}: "
                f"{value!r} — attach tzinfo before passing."
            )
        return value.isoformat()
    return value


def _row_to_run(row: tuple) -> Run:
    (
        id_,
        schedule_id,
        execution_plan_hash,
        fire_reason,
        due_at,
        status_raw,
        attempt,
        root_run_id,
        parent_run_id,
        claimed_by,
        claimed_at,
        started_at,
        completed_at,
        error,
    ) = row
    return Run(
        id=id_,
        schedule_id=schedule_id,
        execution_plan_hash=execution_plan_hash,
        fire_reason=fire_reason,
        due_at=due_at,
        status=RunStatus(status_raw),
        attempt=attempt,
        root_run_id=root_run_id,
        parent_run_id=parent_run_id,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        started_at=started_at,
        completed_at=completed_at,
        error=error,
    )


def insert_run(conn: sqlite3.Connection, run: Run) -> str:
    """Insert a ``Run`` row. Returns ``run.id``.

    All datetime fields (``due_at`` plus the optional
    ``claimed_at`` / ``started_at`` / ``completed_at``) MUST be
    timezone-aware; naive values raise ``NaiveDatetimeError``.

    The helper writes every column the Run model carries —
    including ``claimed_by`` / ``claimed_at`` when set, so a
    test fixture can seed a run in a specific state. That's NOT
    claim semantics; it's neutral storage. Phase 4's worker
    introduces the actual claim transition logic.
    """
    assert_connection_ready(conn)

    conn.execute(
        f"INSERT INTO runs ({', '.join(_RUN_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(_RUN_COLUMNS))})",
        (
            run.id,
            run.schedule_id,
            run.execution_plan_hash,
            run.fire_reason.value,
            _to_iso_or_none(run.due_at, field="due_at"),
            run.status.value,
            run.attempt,
            run.root_run_id,
            run.parent_run_id,
            run.claimed_by,
            _to_iso_or_none(run.claimed_at, field="claimed_at"),
            _to_iso_or_none(run.started_at, field="started_at"),
            _to_iso_or_none(run.completed_at, field="completed_at"),
            run.error,
        ),
    )
    return run.id


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Run]:
    """Return the ``Run`` with the given id, or ``None`` if no
    row matches."""
    assert_connection_ready(conn)
    row = conn.execute(
        f"{_SELECT_SQL} WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_run(row)


def list_pending_due(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
) -> list[Run]:
    """Return pending runs with ``due_at <= now``, ordered by
    ``due_at`` ASC. Read-only.

    This is the query the future wakeup callback iterates to
    decide which runs to hand to the worker. In phase 3 the
    helper is pure SELECT — it never claims, never updates,
    never marks runs as in-flight. Claim semantics are phase
    4's job.

    ``now`` MUST be timezone-aware; naive values raise
    ``NaiveDatetimeError`` so a caller can't accidentally
    compare against a tz-mismatched cursor. ``limit`` is
    forwarded straight to the SQL ``LIMIT`` clause — the caller
    decides the batch size.
    """
    assert_connection_ready(conn)
    if now.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in now: {now!r} — list_pending_due "
            "requires a timezone-aware cursor so the comparison "
            "against stored ISO-with-offset timestamps is well-"
            "defined."
        )

    rows = conn.execute(
        f"{_SELECT_SQL} "
        "WHERE status = ? AND due_at <= ? "
        "ORDER BY due_at ASC LIMIT ?",
        (RunStatus.PENDING.value, now.isoformat(), limit),
    ).fetchall()
    return [_row_to_run(row) for row in rows]


def list_runs_in_chain(
    conn: sqlite3.Connection,
    root_run_id: str,
) -> list[Run]:
    """Return the retry chain rooted at ``root_run_id``,
    ordered by ``attempt`` ASC.

    The first attempt has ``root_run_id == id`` (self-ref per
    design §4.0.4), so the chain always contains at least the
    first attempt's row when ``root_run_id`` matches an
    existing run. Empty list when no rows match.
    """
    assert_connection_ready(conn)
    rows = conn.execute(
        f"{_SELECT_SQL} WHERE root_run_id = ? "
        "ORDER BY attempt ASC",
        (root_run_id,),
    ).fetchall()
    return [_row_to_run(row) for row in rows]


def mark_run_status(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: RunStatus,
    **extra: Any,
) -> None:
    """Unpredicated ``UPDATE runs SET status = ?, <extras> WHERE
    id = ?``.

    NEUTRAL. No source-status guard. No single-flight predicate.
    No claim-ownership check. Phase 4's state machine owns
    legal-transition policy; phase 3 owns only the column write.

    ``extra`` kwargs go through the same allowlist used by
    :func:`update_run_status_and_append_event` to prevent
    arbitrary identifier writes:

    - Allowed: ``started_at``, ``completed_at``, ``error``.
    - Anything else raises :class:`UnknownExtraColumnError`.

    Datetime extras are converted to ISO 8601 strings and
    rejected if naive.

    Raises:
        RunNotFoundError: when UPDATE affects 0 rows.
        UnknownExtraColumnError: ``extra`` carried a forbidden
            key.
        NaiveDatetimeError: a datetime extra was naive.
        ConnectionNotReady: bad connection state.
    """
    assert_connection_ready(conn)

    unknown = set(extra) - ALLOWED_EXTRA_RUN_COLUMNS
    if unknown:
        raise UnknownExtraColumnError(
            f"mark_run_status extra kwargs contain unknown / "
            f"unsafe keys: {sorted(unknown)}. Phase 3 allows "
            f"only {sorted(ALLOWED_EXTRA_RUN_COLUMNS)}; "
            "everything else (``status``, ``attempt``, "
            "``root_run_id``, ``schedule_id``, ``claimed_by`` / "
            "``claimed_at``, etc.) is rejected — the helper "
            "interpolates these keys directly into SQL and must "
            "work from a fixed allowlist."
        )

    coerced = {
        k: _coerce_extra_value(v, field=k) for k, v in extra.items()
    }

    extra_keys = sorted(coerced)
    set_columns = ["status = ?"] + [f"{k} = ?" for k in extra_keys]
    values: list[Any] = [status.value] + [coerced[k] for k in extra_keys]
    values.append(run_id)

    cursor = conn.execute(
        f"UPDATE runs SET {', '.join(set_columns)} WHERE id = ?",
        values,
    )
    if cursor.rowcount == 0:
        raise RunNotFoundError(
            f"runs row {run_id!r} not found — UPDATE affected 0 rows."
        )


__all__ = [
    "ALLOWED_EXTRA_RUN_COLUMNS",
    "insert_run",
    "get_run",
    "list_pending_due",
    "list_runs_in_chain",
    "mark_run_status",
]
