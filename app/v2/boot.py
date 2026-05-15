"""v2 scheduler top-level boot wiring for ``run_bot.py``.

Phase 9 slice 7 per ``docs/PHASE_9_PLAN.md`` §3.7.

This module is the single seam where ``run_bot.py`` reaches
into the v2 stack to bring it online alongside v1. The v2
storage layer expects an open ``sqlite3.Connection`` per
call; this module owns the file path, the per-connection
pragma setup, and the migration runner -- callers below
just receive a ``RuntimeHandle`` they can ``activate()``
when transports are ready.

Boot sequence:

1. Resolve the SQLite file path (caller-supplied; defaults
   to ``data/scheduler-v2-state.db``).
2. Open a one-shot migration connection, set the required
   pragmas, and call
   :func:`app.v2.migrations.runner.apply_pending` so the
   first deploy on a fresh box self-bootstraps the schema.
3. Build a :func:`conn_factory` closure that opens a
   per-call connection with ``PRAGMA foreign_keys=ON`` set.
4. Call :func:`app.v2.runtime.boot.boot_runtime` with
   ``autostart=False`` so the binding stays paused and
   workers stay unstarted until the caller drives
   :meth:`RuntimeHandle.activate`.

The wrapper deliberately does NOT touch the APScheduler
jobstore path -- that defaults to phase-5's
``sqlite:///data/scheduler-v2-jobs.db`` and is passed
through unchanged (round-1 reviewer Q9).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from app.v2.migrations.runner import apply_pending
from app.v2.runtime.boot import RuntimeHandle, boot_runtime


_logger = logging.getLogger(__name__)


def _ensure_parent_dir(db_path: str) -> None:
    """Create the parent directory for ``db_path`` if it
    does not exist. Idempotent."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)


def _open_conn(db_path: str) -> sqlite3.Connection:
    """Open a new SQLite connection with the per-connection
    pragmas the v2 storage layer requires.

    ``PRAGMA foreign_keys`` is per-connection in SQLite, so
    every conn opened against the v2 state DB must set it
    independently. ``isolation_level=None`` is required by
    :mod:`app.v2.storage.transactions` so the transaction
    context manager can drive BEGIN / COMMIT / ROLLBACK
    explicitly (autocommit at the driver layer).
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def boot_v2_runtime(db_path: str) -> RuntimeHandle:
    """Bootstrap the v2 scheduler from ``run_bot.py``.

    Args:
        db_path: Absolute or repo-relative path to the
            SQLite file backing the v2 ``schedules`` /
            ``runs`` / ``events`` tables. Created + WAL-
            migrated on first boot.

    Returns: A :class:`RuntimeHandle` with
        ``_activated=False``. The caller MUST drive
        :meth:`RuntimeHandle.activate` once Slack /
        Telegram transports have registered their
        adapters; otherwise the binding stays paused
        and no workers poll.
    """
    _ensure_parent_dir(db_path)

    # One-shot migration connection. The runner is
    # idempotent: a fresh DB self-bootstraps via the
    # ``applied_migrations`` bookkeeping table; a
    # subsequent boot with no new migrations is a no-op.
    migration_conn = _open_conn(db_path)
    try:
        applied = apply_pending(migration_conn)
        if applied:
            _logger.info(
                "v2 migrations applied: %s",
                ", ".join(applied),
            )
    finally:
        migration_conn.close()

    def conn_factory() -> sqlite3.Connection:
        return _open_conn(db_path)

    handle = await boot_runtime(conn_factory, autostart=False)
    return handle


__all__ = ["boot_v2_runtime"]
