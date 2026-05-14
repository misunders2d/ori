"""Forward-only SQLite migration runner for the v2 scheduler.

Contract per ``docs/PHASE_1_PLAN.md`` §3.1 + §3.5:

- ``apply_pending(conn)`` is idempotent: re-runs skip migrations
  already in ``applied_migrations``.
- Pre-migration setup runs OUTSIDE any transaction:
    1. ``PRAGMA journal_mode=WAL`` (SQLite forbids it inside TX).
       Skipped if already in WAL mode.
    2. ``PRAGMA foreign_keys=ON`` (per-connection).
    3. ``CREATE TABLE IF NOT EXISTS applied_migrations`` — the
       bookkeeping table the runner owns. Individual migrations
       never touch it.
- Per-migration: ``BEGIN; migration.apply(conn);
  INSERT INTO applied_migrations; COMMIT`` — atomic. Failure
  triggers ``ROLLBACK`` and re-raises.

Phase 1 enforces that the runner is only ever called against
test DBs (see ``docs/PHASE_1_PLAN.md`` §3.3); production wiring
lands in phase 4. The caller passes an explicit connection — the
runner never opens ``data/ori-scheduler.db`` itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.v2.migrations import Migration
from app.v2.migrations.v001_initial import V001Initial


# Ordered list of all known migrations. Append only; never
# reorder and never delete entries — once a migration ships to
# any environment its id is permanent in ``applied_migrations``.
MIGRATIONS: list[Migration] = [V001Initial()]


_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS applied_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
""".strip()


def _ensure_autocommit(conn: sqlite3.Connection) -> None:
    """Put the connection in explicit-TX mode.

    The legacy Python sqlite3 module has an ``isolation_level``
    that triggers an implicit ``BEGIN`` before any data-modifying
    statement and silently commits before DDL. Setting it to
    ``None`` disables that behavior — the runner now owns the TX
    boundary explicitly, which is the contract we need to make
    WAL setup + per-migration atomicity correct.

    Mutating ``isolation_level`` is per-connection state, not
    global. The caller passed us their connection; for phase 1
    that's a per-test ephemeral connection.
    """
    conn.isolation_level = None


def _enable_wal_outside_tx(conn: sqlite3.Connection) -> None:
    """Set ``journal_mode=WAL`` only if the DB isn't already
    in WAL mode.

    SQLite raises if asked to change the journal mode while a
    transaction is open — that's why this runs in the
    pre-migration phase, not inside ``apply``.
    """
    row = conn.execute("PRAGMA journal_mode").fetchone()
    current = (row[0] if row else "").lower()
    if current != "wal":
        conn.execute("PRAGMA journal_mode=WAL")


def _bootstrap_bookkeeping(conn: sqlite3.Connection) -> None:
    """Create the ``applied_migrations`` table if absent.

    Runs OUTSIDE any per-migration transaction so the bookkeeping
    table is guaranteed to exist before the runner queries it via
    :func:`applied_ids`. Idempotent — ``IF NOT EXISTS`` is cheap
    and safe to re-run on every call.
    """
    conn.execute(_BOOTSTRAP_SQL)


def applied_ids(conn: sqlite3.Connection) -> set[str]:
    """Return the set of ids in ``applied_migrations``.

    Returns an empty set if the table does not yet exist — a
    fresh DB before :func:`apply_pending` has bootstrapped it.
    """
    try:
        rows = conn.execute("SELECT id FROM applied_migrations").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row[0] for row in rows}


def apply_pending(
    conn: sqlite3.Connection,
    migrations: Optional[list[Migration]] = None,
) -> list[str]:
    """Apply every migration not already in ``applied_migrations``.

    ``migrations`` defaults to the module-level :data:`MIGRATIONS`
    list. Tests pass a custom list to exercise edge cases (mid-
    apply failure, ordering) without touching the canonical list.

    Returns the ids of migrations newly applied in this call, in
    the order they ran. A re-run on an already-up-to-date DB
    returns ``[]`` and writes nothing.

    On a per-migration failure (the migration's ``apply`` raises
    OR the bookkeeping INSERT raises), the runner issues
    ``ROLLBACK`` and re-raises. The DB is left exactly as it was
    before that migration's ``BEGIN``. Subsequent migrations in
    the list are NOT attempted — keep the chain monotonic.
    """
    plan = MIGRATIONS if migrations is None else migrations

    _ensure_autocommit(conn)
    _enable_wal_outside_tx(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    _bootstrap_bookkeeping(conn)

    already = applied_ids(conn)
    newly_applied: list[str] = []
    for migration in plan:
        if migration.id in already:
            continue
        conn.execute("BEGIN")
        try:
            migration.apply(conn)
            conn.execute(
                "INSERT INTO applied_migrations (id, applied_at) VALUES (?, ?)",
                (migration.id, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        newly_applied.append(migration.id)
    return newly_applied


__all__ = ["MIGRATIONS", "apply_pending", "applied_ids"]
