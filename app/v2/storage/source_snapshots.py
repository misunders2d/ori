"""CRUD over the ``source_snapshots`` table.

Phase 3 slice 8 per ``docs/PHASE_3_PLAN.md`` §5.6.

Three helpers:

- :func:`insert_snapshot` — write a Pydantic
  ``SourceSnapshotMetadata`` row. ``fetched_at`` is rejected if
  naive and UTC-normalised on the way in.
- :func:`get_snapshot` — read a row by composite PK
  ``(run_id, source_id)``.
- :func:`list_snapshots_by_hash` — return every snapshot row
  with a given ``content_hash``, ordered chronologically. Used
  by the audit / dedup tools to find every fire that observed
  the same source content.

No update / delete surface — source snapshots are
content-addressed historical records. A new snapshot is a new
row.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2, §4.6.4
- ``docs/PHASE_3_PLAN.md`` §5.6
"""

from __future__ import annotations

import sqlite3
from datetime import timezone
from typing import Optional

from app.v2.enums import SelectionMethod
from app.v2.models.snapshot import SourceSnapshotMetadata
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError


_COLUMNS = (
    "run_id",
    "source_id",
    "content_hash",
    "content_path",
    "content_size",
    "fetched_at",
    "source_kind",
    "source_version",
    "selection_method",
)
_SELECT_SQL = f"SELECT {', '.join(_COLUMNS)} FROM source_snapshots"


def _row_to_meta(row: tuple) -> SourceSnapshotMetadata:
    (
        run_id,
        source_id,
        content_hash,
        content_path,
        content_size,
        fetched_at,
        source_kind,
        source_version,
        selection_method_raw,
    ) = row
    return SourceSnapshotMetadata(
        run_id=run_id,
        source_id=source_id,
        content_hash=content_hash,
        content_path=content_path,
        content_size=content_size,
        fetched_at=fetched_at,
        source_kind=source_kind,
        source_version=source_version,
        selection_method=SelectionMethod(selection_method_raw),
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    meta: SourceSnapshotMetadata,
) -> tuple[str, str]:
    """Insert a ``SourceSnapshotMetadata`` row. Returns the
    composite PK ``(run_id, source_id)``.

    ``meta.fetched_at`` MUST be tz-aware; naive raises
    ``NaiveDatetimeError``. Stored value is UTC-normalised
    matching the rest of the storage layer.

    Raises:
        NaiveDatetimeError: ``fetched_at`` was naive.
        ConnectionNotReady: bad connection state.
        sqlite3.IntegrityError: duplicate composite PK OR
            ``run_id`` references a non-existent run (FK).
    """
    assert_connection_ready(conn)
    if meta.fetched_at.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in meta.fetched_at: "
            f"{meta.fetched_at!r} — attach tzinfo (typically "
            "datetime.timezone.utc) before passing."
        )
    fetched_at_iso = meta.fetched_at.astimezone(timezone.utc).isoformat()

    conn.execute(
        f"INSERT INTO source_snapshots ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join(['?'] * len(_COLUMNS))})",
        (
            meta.run_id,
            meta.source_id,
            meta.content_hash,
            meta.content_path,
            meta.content_size,
            fetched_at_iso,
            meta.source_kind,
            meta.source_version,
            meta.selection_method.value,
        ),
    )
    return (meta.run_id, meta.source_id)


def get_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_id: str,
) -> Optional[SourceSnapshotMetadata]:
    """Return the snapshot for ``(run_id, source_id)`` or
    ``None``."""
    assert_connection_ready(conn)
    row = conn.execute(
        f"{_SELECT_SQL} WHERE run_id = ? AND source_id = ?",
        (run_id, source_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_meta(row)


def list_snapshots_by_hash(
    conn: sqlite3.Connection,
    content_hash: str,
) -> list[SourceSnapshotMetadata]:
    """Return every snapshot with the given ``content_hash``,
    ordered ASC by ``(fetched_at, run_id, source_id)``.

    Use case: the audit tool wants every fire that ever
    observed the same source content. Chronological order
    surfaces "first seen", "last seen", and any gaps in the
    middle. The secondary sort keys make the ordering
    deterministic even when two snapshots share a
    millisecond-resolution timestamp.

    Empty list when no rows match (no error).
    """
    assert_connection_ready(conn)
    rows = conn.execute(
        f"{_SELECT_SQL} WHERE content_hash = ? "
        "ORDER BY fetched_at ASC, run_id ASC, source_id ASC",
        (content_hash,),
    ).fetchall()
    return [_row_to_meta(row) for row in rows]


__all__ = [
    "insert_snapshot",
    "get_snapshot",
    "list_snapshots_by_hash",
]
