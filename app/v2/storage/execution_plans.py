"""CRUD over the ``execution_plans`` table.

Phase 3 slice 4 per ``docs/PHASE_3_PLAN.md`` §5.2.

Plans are immutable. The surface is intentionally narrow —
``insert_execution_plan`` + ``get_execution_plan``. There is no
``update_*`` and no ``delete_*``: a "revision" is a new plan
with a different hash, inserted via :func:`insert_execution_plan`
and pointed at by a new ``ScheduleSpec.execution_plan_hash``.

Storage shape (per DDL §4.0.2):

- ``hash``        — primary key, identical to ``plan.hash``.
- ``body_json``   — the entire ExecutionPlan serialised via the
  deterministic encoder. Round-trip is via Pydantic
  ``model_validate_json``.
- ``enforcement`` — duplicated out of the body for the CHECK
  constraint + future indexed-filter lookups.
- ``authored_at`` — duplicated for chronological listing.
- ``author``      — duplicated for per-author observability.

The duplication is intentional: the body is the canonical
record; the columns are indexable views into the same data.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2
- ``docs/PHASE_3_PLAN.md`` §5.2
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from app.v2.models.execution_plan import ExecutionPlan
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import decode_json, encode_json


class ExecutionPlanNotFrozenError(ValueError):
    """Raised by :func:`insert_execution_plan` when
    ``plan.hash`` is empty.

    Plans must be frozen (``with_fresh_hash()`` called) before
    they land in storage — the hash IS the primary key and the
    addressable handle other tables FK against.
    """


def insert_execution_plan(
    conn: sqlite3.Connection,
    plan: ExecutionPlan,
) -> str:
    """Insert a frozen ``ExecutionPlan``. Returns ``plan.hash``.

    Raises:
        ExecutionPlanNotFrozenError: ``plan.hash`` empty.
        ConnectionNotReady: bad connection state.
        sqlite3.IntegrityError: duplicate hash. Plans are
            content-addressed, so a duplicate hash means the
            same body was inserted twice — the caller should
            ``get_execution_plan(hash_)`` first if it wants the
            existing row.

    The helper does NOT verify ``plan.hash == plan.compute_hash()``;
    that's the phase-2 validator's job. Storage trusts the
    upstream contract that a frozen plan has the correct hash.
    """
    assert_connection_ready(conn)

    if not plan.hash:
        raise ExecutionPlanNotFrozenError(
            f"ExecutionPlan.hash is empty for id={plan.id!r}. "
            "Call plan.with_fresh_hash() before inserting."
        )

    conn.execute(
        "INSERT INTO execution_plans "
        "(hash, body_json, enforcement, authored_at, author) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            plan.hash,
            encode_json(plan),
            plan.enforcement.value,
            plan.authored_at,
            plan.author,
        ),
    )
    return plan.hash


def get_execution_plan(
    conn: sqlite3.Connection,
    hash_: str,
) -> Optional[ExecutionPlan]:
    """Return the ``ExecutionPlan`` with the given hash, or
    ``None`` if no row matches.

    Pure read. ``body_json`` is the authoritative source — the
    helper does NOT cross-check the indexed columns against the
    decoded body; that would be redundant with the writer's
    guarantees and slow the hot path. A future replay-style
    audit tool can do the cross-check separately.
    """
    assert_connection_ready(conn)
    row = conn.execute(
        "SELECT body_json FROM execution_plans WHERE hash = ?",
        (hash_,),
    ).fetchone()
    if row is None:
        return None
    return decode_json(row[0], ExecutionPlan)


__all__ = [
    "ExecutionPlanNotFrozenError",
    "insert_execution_plan",
    "get_execution_plan",
]
