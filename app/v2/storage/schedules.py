"""CRUD over the ``schedules`` table.

Phase 3 slice 3 surface per ``docs/PHASE_3_PLAN.md`` §5.1:

- :func:`insert_schedule` — write a frozen ``ScheduleSpec`` to
  the table. Caller is expected to have called
  ``spec.with_fresh_hash()`` first; :class:`ScheduleNotFrozenError`
  is raised on empty hash so the storage layer never persists
  a draft.
- :func:`get_schedule` — read a row back into a typed
  ``ScheduleSpec``.
- :func:`list_active_schedules` — read every row where
  ``status='active'``.
- :func:`update_schedule_status` — flip ``status`` for an
  existing row. :class:`ScheduleNotFoundError` on miss.

Pure data plane. No runtime / worker / wakeup / claim. No
prod-DB access — the caller passes the SQLite connection.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2 (DDL)
- ``docs/PHASE_3_PLAN.md`` §5.1
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from pydantic import TypeAdapter

from app.v2.enums import ScheduleStatus
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import Trigger
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import decode_json, encode_json


class ScheduleNotFoundError(LookupError):
    """Raised by :func:`update_schedule_status` and any other
    helper that requires the schedule row to exist."""


class ScheduleNotFrozenError(ValueError):
    """Raised by :func:`insert_schedule` when ``spec.hash`` is
    empty. A draft ScheduleSpec has no business in the
    ``schedules`` table — the freeze pathway in a later phase
    will populate ``hash`` before this helper is called.
    """


# ``Trigger`` is a discriminated union, not a single BaseModel
# subclass — Pydantic's ``model_validate_json`` works only on
# classes, so we hold a TypeAdapter at module load time and use
# it to deserialize the column.
_TRIGGER_ADAPTER: TypeAdapter[Trigger] = TypeAdapter(Trigger)


_COLUMNS = (
    "id",
    "owner",
    "description",
    "trigger_json",
    "delivery_json",
    "failure_json",
    "audit_json",
    "status",
    "execution_plan_hash",
    "template_json",
    "authored_at",
    "parent_hash",
    "hash",
)
_SELECT_SQL = (
    f"SELECT {', '.join(_COLUMNS)} FROM schedules"
)


def _row_to_spec(row: tuple) -> ScheduleSpec:
    """Reconstruct a ``ScheduleSpec`` from a database row in
    the column order defined by :data:`_COLUMNS`.

    ``trigger`` uses the TypeAdapter so the discriminated union
    deserialises cleanly. Other Pydantic sub-objects use the
    generic :func:`decode_json`. ``template_json`` may be NULL
    (column is nullable in the DDL).
    """
    (
        id_,
        owner_raw,
        description,
        trigger_raw,
        delivery_raw,
        failure_raw,
        audit_raw,
        status_raw,
        execution_plan_hash,
        template_raw,
        authored_at,
        parent_hash,
        hash_,
    ) = row

    template = (
        decode_json(template_raw, TemplateRef)
        if template_raw is not None
        else None
    )

    return ScheduleSpec(
        id=id_,
        owner=decode_json(owner_raw, UserRef),
        description=description,
        trigger=_TRIGGER_ADAPTER.validate_json(trigger_raw),
        delivery=decode_json(delivery_raw, Delivery),
        failure=decode_json(failure_raw, FailurePolicy),
        audit=decode_json(audit_raw, AuditPolicy),
        status=ScheduleStatus(status_raw),
        execution_plan_hash=execution_plan_hash,
        template=template,
        authored_at=authored_at,
        parent_hash=parent_hash,
        hash=hash_,
    )


def insert_schedule(
    conn: sqlite3.Connection,
    spec: ScheduleSpec,
) -> str:
    """Insert a frozen ``ScheduleSpec``. Returns ``spec.id``.

    Raises:
        ScheduleNotFrozenError: ``spec.hash`` is empty (call
            ``spec.with_fresh_hash()`` at freeze time first).
        ConnectionNotReady: connection missing WAL /
            foreign_keys / v001 migration.
        sqlite3.IntegrityError: id / hash unique constraint or
            status CHECK constraint violation.

    The helper does NOT recompute the hash — the spec arrives
    with its intended canonical hash already populated. The
    phase-2 validator
    (``app.v2.validation.validate_schedule_spec``) is the
    chokepoint that checks ``spec.hash == spec.compute_hash()``
    before this helper sees the spec.
    """
    assert_connection_ready(conn)

    if not spec.hash:
        raise ScheduleNotFrozenError(
            f"ScheduleSpec.hash is empty for id={spec.id!r}. The "
            "storage layer never persists a draft — call "
            "spec.with_fresh_hash() before inserting."
        )

    template_value = (
        encode_json(spec.template) if spec.template is not None else None
    )

    conn.execute(
        "INSERT INTO schedules "
        f"({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(_COLUMNS))})",
        (
            spec.id,
            encode_json(spec.owner),
            spec.description,
            encode_json(spec.trigger),
            encode_json(spec.delivery),
            encode_json(spec.failure),
            encode_json(spec.audit),
            spec.status.value,
            spec.execution_plan_hash,
            template_value,
            spec.authored_at,
            spec.parent_hash,
            spec.hash,
        ),
    )
    return spec.id


def get_schedule(
    conn: sqlite3.Connection,
    schedule_id: str,
) -> Optional[ScheduleSpec]:
    """Return the ``ScheduleSpec`` with the given id, or
    ``None`` if no row matches.

    Pure read — no mutation, no transaction. ``assert_connection_ready``
    still runs to fail loud if the caller passed an unprepared
    connection.
    """
    assert_connection_ready(conn)
    row = conn.execute(
        f"{_SELECT_SQL} WHERE id = ?",
        (schedule_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_spec(row)


def list_active_schedules(conn: sqlite3.Connection) -> list[ScheduleSpec]:
    """Return every ScheduleSpec where ``status='active'``.

    Order is by ``id`` ascending so the output is deterministic
    for callers that snapshot the list for diffing. ``paused`` /
    ``archived`` rows are intentionally excluded — the wakeup
    function (phase 4) iterates only the active set, and any
    diagnostic listing that wants the others can call
    ``get_schedule`` per id or write its own query.
    """
    assert_connection_ready(conn)
    rows = conn.execute(
        f"{_SELECT_SQL} WHERE status = ? ORDER BY id ASC",
        (ScheduleStatus.ACTIVE.value,),
    ).fetchall()
    return [_row_to_spec(row) for row in rows]


def update_schedule_status(
    conn: sqlite3.Connection,
    schedule_id: str,
    status: ScheduleStatus,
) -> None:
    """Flip a schedule's ``status`` column.

    Raises:
        ScheduleNotFoundError: when no row has ``id = schedule_id``.
        sqlite3.IntegrityError: when ``status`` is somehow
            outside the CHECK set (shouldn't happen with the
            enum-bounded argument, but the DDL is the last
            line of defense).
    """
    assert_connection_ready(conn)
    cursor = conn.execute(
        "UPDATE schedules SET status = ? WHERE id = ?",
        (status.value, schedule_id),
    )
    if cursor.rowcount == 0:
        raise ScheduleNotFoundError(
            f"schedules row {schedule_id!r} not found — UPDATE "
            "affected 0 rows."
        )


__all__ = [
    "ScheduleNotFoundError",
    "ScheduleNotFrozenError",
    "insert_schedule",
    "get_schedule",
    "list_active_schedules",
    "update_schedule_status",
]
