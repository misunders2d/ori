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

``insert_snapshot`` is append-only + content-addressed (a
new snapshot is a new row). The ONLY sanctioned deletion
is retention (phase-10 slice 4, design §5.3.5 / plan
§3.4): :func:`delete_snapshot` removes one pruned row and
:func:`list_snapshots_for_schedule_source` /
:func:`content_path_referenced` are its read primitives.
The orchestration — COMMIT the row deletes FIRST, then
post-commit best-effort file unlink on a FRESH connection
(re-query so a dedup-shared file is unlinked only when its
last referencing row is gone) — lives in
:mod:`app.v2.sources.snapshot_writer.prune_snapshots`,
NEVER inside the delete transaction (a rollback would
otherwise restore rows pointing at already-deleted files).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2, §4.6.4, §5.3.5
- ``docs/PHASE_3_PLAN.md`` §5.6
- ``docs/PHASE_10_PLAN.md`` §3.4
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


# ---------------------------------------------------------------------------
# Retention primitives (phase-10 slice 4 — the SOLE
# sanctioned delete surface; orchestrated by
# app.v2.sources.snapshot_writer.prune_snapshots).
# ---------------------------------------------------------------------------


def list_snapshots_for_schedule_source(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    source_id: str,
) -> list[tuple[str, str, str]]:
    """Return ``(run_id, source_id, content_path)`` for every
    snapshot of ``source_id`` under ``schedule_id``,
    **newest first** (``fetched_at`` DESC, then ``run_id``
    DESC for a deterministic tie-break).

    ``schedule_id`` is resolved via ``JOIN runs ON
    runs.id = source_snapshots.run_id`` — the
    ``source_snapshots`` table has no ``schedule_id``
    column (PK is ``(run_id, source_id)``).
    """
    assert_connection_ready(conn)
    rows = conn.execute(
        "SELECT s.run_id, s.source_id, s.content_path "
        "FROM source_snapshots s "
        "JOIN runs r ON r.id = s.run_id "
        "WHERE r.schedule_id = ? AND s.source_id = ? "
        "ORDER BY s.fetched_at DESC, s.run_id DESC",
        (schedule_id, source_id),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def delete_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_id: str,
) -> int:
    """Delete one snapshot row by composite PK. Returns the
    number of rows removed (0 or 1). Retention-only — the
    caller drives the COMMIT-then-unlink ordering."""
    assert_connection_ready(conn)
    cur = conn.execute(
        "DELETE FROM source_snapshots "
        "WHERE run_id = ? AND source_id = ?",
        (run_id, source_id),
    )
    return cur.rowcount


def content_path_referenced(
    conn: sqlite3.Connection,
    content_path: str,
) -> bool:
    """True iff ANY surviving snapshot row still references
    ``content_path``. The post-commit unlink uses this on a
    FRESH connection so a dedup-shared file is removed only
    when its LAST referencing row is gone."""
    assert_connection_ready(conn)
    row = conn.execute(
        "SELECT 1 FROM source_snapshots "
        "WHERE content_path = ? LIMIT 1",
        (content_path,),
    ).fetchone()
    return row is not None


__all__ = [
    "insert_snapshot",
    "get_snapshot",
    "list_snapshots_by_hash",
    "list_snapshots_for_schedule_source",
    "delete_snapshot",
    "content_path_referenced",
]
