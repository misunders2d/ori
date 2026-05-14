"""Caller-passed SQLite connection contract for the v2 storage
layer.

Every storage helper takes an explicit ``sqlite3.Connection``.
No helper opens its own connection or imports
``data/ori-scheduler.db`` — production wiring is phase 4's job.

The :func:`assert_connection_ready` helper validates the
caller's connection has the expected pragmas + schema baseline
before the storage layer mutates anything. Mutating helpers
call this at function entry; the cost is two ``PRAGMA`` reads
plus one ``SELECT`` — cheap enough to leave on in tests.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2 (DDL), §4.0.5
- ``docs/PHASE_3_PLAN.md`` §3
"""

from __future__ import annotations

import sqlite3


class ConnectionNotReady(RuntimeError):
    """Raised by :func:`assert_connection_ready` when the
    connection is missing one of the required pragmas or the
    v001 migration row is absent.

    Subclass of ``RuntimeError`` rather than ``ValueError``
    because the failure is environmental (caller wired the
    connection wrong) rather than an argument-shape problem.
    """


_REQUIRED_MIGRATION_ID = "v001_initial"


def assert_connection_ready(conn: sqlite3.Connection) -> None:
    """Validate ``conn`` is ready to serve as a v2 storage target.

    Raises :class:`ConnectionNotReady` when any of the following
    is false:

    - ``PRAGMA journal_mode`` is ``wal`` (set by the migration
      runner).
    - ``PRAGMA foreign_keys`` is on (per-connection — every
      new connection needs to set this independently).
    - The ``applied_migrations`` table exists AND contains the
      ``v001_initial`` row.

    The check is intentionally minimal — it does not introspect
    every business table, just confirms the migration baseline
    was applied. CHECK / FK / NOT NULL constraints inside the
    business tables enforce themselves at INSERT time, so the
    storage layer doesn't need to re-check shape here.
    """
    journal_row = conn.execute("PRAGMA journal_mode").fetchone()
    journal_mode = (journal_row[0] if journal_row else "").lower()
    if journal_mode != "wal":
        raise ConnectionNotReady(
            f"PRAGMA journal_mode must be 'wal'; got "
            f"{journal_mode!r}. Run the v2 migration runner "
            "before opening the storage layer "
            "(app.v2.migrations.runner.apply_pending)."
        )

    foreign_keys_row = conn.execute("PRAGMA foreign_keys").fetchone()
    foreign_keys_on = bool(foreign_keys_row and foreign_keys_row[0] == 1)
    if not foreign_keys_on:
        raise ConnectionNotReady(
            "PRAGMA foreign_keys must be ON on this connection. "
            "The migration runner sets it on the connection it "
            "runs against; new connections to the same DB file "
            "must execute `PRAGMA foreign_keys=ON` independently."
        )

    try:
        rows = conn.execute(
            "SELECT id FROM applied_migrations WHERE id = ?",
            (_REQUIRED_MIGRATION_ID,),
        ).fetchall()
    except sqlite3.OperationalError as e:
        raise ConnectionNotReady(
            "applied_migrations table is absent — the v2 "
            "migration runner has not been applied to this "
            f"database (sqlite said: {e})."
        ) from e

    if not rows:
        raise ConnectionNotReady(
            f"applied_migrations exists but {_REQUIRED_MIGRATION_ID!r} "
            "is not recorded. The schema baseline is missing — "
            "run the migration runner before exercising the "
            "storage layer."
        )


__all__ = ["ConnectionNotReady", "assert_connection_ready"]
