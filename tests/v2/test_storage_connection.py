"""Tests for ``app.v2.storage.connection``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.1:

- ``assert_connection_ready`` passes on a fresh migrated DB.
- Fails when ``journal_mode != wal``.
- Fails when ``foreign_keys`` is off (new connection didn't
  re-set the per-connection pragma).
- Fails when ``applied_migrations`` table is absent.
- Fails when ``applied_migrations`` exists but the
  ``v001_initial`` row is missing.

Cross-cutting smoke (carried from phase 2 pattern):
- Module does not import I/O libraries (sqlite3 is the
  exception — storage layer needs it; httpx / requests /
  slack_sdk / etc. forbidden).
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from app.v2.migrations import runner
from app.v2.storage import connection as connection_mod
from app.v2.storage.connection import ConnectionNotReady, assert_connection_ready


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(tmp_path / "scheduler.db"))


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    """Open + migrate a fresh DB. The migration runner sets
    journal_mode=WAL, foreign_keys=ON, and writes the v001 row."""
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    return conn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_assert_passes_on_fresh_migrated_connection(tmp_path):
    conn = _migrate(tmp_path)
    # Should not raise.
    assert_connection_ready(conn)


# ---------------------------------------------------------------------------
# journal_mode check
# ---------------------------------------------------------------------------


def test_assert_fails_when_journal_mode_is_not_wal(tmp_path):
    """Brand-new DB starts in 'delete' mode. The runner switches
    to WAL; if we never run the runner, the assertion must
    refuse."""
    conn = _open(tmp_path)
    # Don't run migration. Just create the bookkeeping table by
    # hand so the FK / applied_migrations checks don't fire
    # first (we want this test to isolate the journal_mode check).
    conn.execute(
        "CREATE TABLE applied_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO applied_migrations (id, applied_at) "
        "VALUES ('v001_initial', '2026-05-15T00:00:00+00:00')"
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ConnectionNotReady, match="journal_mode"):
        assert_connection_ready(conn)


# ---------------------------------------------------------------------------
# foreign_keys check
# ---------------------------------------------------------------------------


def test_assert_fails_when_foreign_keys_off_on_new_connection(tmp_path):
    """The migration runner enables foreign_keys on ITS
    connection — but new connections opened against the same DB
    file have foreign_keys=OFF by default. Verify the assertion
    catches that."""
    # Migrate once via runner; close.
    conn = _migrate(tmp_path)
    conn.close()

    # Open a fresh connection — foreign_keys defaults to OFF.
    fresh = sqlite3.connect(str(tmp_path / "scheduler.db"))
    fresh.row_factory = None
    fk_row = fresh.execute("PRAGMA foreign_keys").fetchone()
    assert fk_row[0] == 0  # sanity: default off

    with pytest.raises(ConnectionNotReady, match="foreign_keys"):
        assert_connection_ready(fresh)


def test_assert_passes_when_fresh_connection_sets_foreign_keys(tmp_path):
    """The fresh-conn path is recoverable: callers set the
    pragma themselves and the assertion accepts it."""
    conn = _migrate(tmp_path)
    conn.close()

    fresh = sqlite3.connect(str(tmp_path / "scheduler.db"))
    fresh.execute("PRAGMA foreign_keys=ON")
    assert_connection_ready(fresh)


# ---------------------------------------------------------------------------
# applied_migrations checks
# ---------------------------------------------------------------------------


def test_assert_fails_when_applied_migrations_absent(tmp_path):
    """A manually-created DB with no migration history fails
    cleanly, not with an obscure OperationalError."""
    conn = _open(tmp_path)
    # Get WAL + foreign_keys right so we isolate the migration
    # check.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    with pytest.raises(ConnectionNotReady, match="applied_migrations"):
        assert_connection_ready(conn)


def test_assert_fails_when_v001_row_missing(tmp_path):
    """Bookkeeping table exists but no migration has run.
    Distinct error wording from the absent-table case so an
    operator can tell them apart."""
    conn = _open(tmp_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "CREATE TABLE applied_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )

    with pytest.raises(ConnectionNotReady, match="v001_initial"):
        assert_connection_ready(conn)


# ---------------------------------------------------------------------------
# Error class metadata
# ---------------------------------------------------------------------------


def test_connection_not_ready_is_runtime_error():
    """The failure is environmental (caller wired the
    connection wrong), so it inherits from ``RuntimeError``,
    not ``ValueError``. Tests pin the hierarchy so a future
    refactor doesn't silently break ``except RuntimeError``
    catches."""
    assert issubclass(ConnectionNotReady, RuntimeError)


# ---------------------------------------------------------------------------
# Smoke checks — module is data-plane, not runtime
# ---------------------------------------------------------------------------


def test_connection_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(connection_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"app.v2.storage.connection imports I/O libs implying "
        f"runtime: {sorted(leaked)}."
    )


def test_connection_module_has_no_dispatch_callables():
    """Module exposes only `assert_connection_ready` +
    `ConnectionNotReady`. Anything named `dispatch` / `invoke`
    / `call` / `execute` / `run` / `send` / `start` / `loop`
    would be a phase-3-boundary violation."""
    forbidden = {
        "dispatch",
        "invoke",
        "call",
        "execute",
        "run",
        "send",
        "start",
        "loop",
        "worker",
    }
    for name, member in vars(connection_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"connection module exposes execution-suggestive "
                f"callable: {name}"
            )
