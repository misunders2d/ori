"""Tests for ``app.v2.migrations.runner``.

Pins per ``docs/PHASE_1_PLAN.md`` §5.3:

- Fresh DB: ``apply_pending`` runs v001, returns
  ``['v001_initial']``.
- Re-apply on an already-migrated DB returns ``[]`` and changes
  nothing.
- Mid-apply failure rolls back: ``applied_migrations`` does NOT
  contain the failed id; any business table the migration
  partially created is gone.
- ``applied_migrations`` is created before any other CREATE TABLE.

The runner is invoked against per-test ephemeral SQLite DBs at
``tmp_path``. Phase 1 is test-DB-only — no production DB access.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

import pytest

from app.v2.migrations import runner
from app.v2.migrations.v001_initial import V001Initial


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    """Open a brand-new SQLite DB file. The runner takes
    ownership of isolation_level + pragmas."""
    return sqlite3.connect(str(tmp_path / "scheduler.db"))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


# All six business tables created by v001.
_V001_TABLES = {
    "schedules",
    "execution_plans",
    "runs",
    "events",
    "schedule_state",
    "source_snapshots",
}


# ---------------------------------------------------------------------------
# Fresh DB: full apply
# ---------------------------------------------------------------------------


def test_fresh_db_applies_v001(tmp_path):
    conn = _open(tmp_path)
    newly_applied = runner.apply_pending(conn)
    assert newly_applied == ["v001_initial"]


def test_fresh_apply_creates_all_business_tables(tmp_path):
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    present = _table_names(conn)
    for tbl in _V001_TABLES:
        assert tbl in present, f"v001 didn't create {tbl}"


def test_fresh_apply_creates_bookkeeping_table(tmp_path):
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    assert "applied_migrations" in _table_names(conn)


def test_fresh_apply_records_id_in_bookkeeping(tmp_path):
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    ids = runner.applied_ids(conn)
    assert ids == {"v001_initial"}


def test_fresh_apply_enables_foreign_keys(tmp_path):
    """The runner sets ``PRAGMA foreign_keys=ON`` on the
    connection. Without that, FK constraints in the DDL are
    parsed but not enforced — silently broken."""
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_fresh_apply_sets_wal_journal_mode(tmp_path):
    """WAL is required for the concurrent-reader / single-writer
    model the worker pool relies on."""
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0].lower() == "wal"


# ---------------------------------------------------------------------------
# Re-apply is a no-op
# ---------------------------------------------------------------------------


def test_reapply_on_migrated_db_is_noop(tmp_path):
    conn = _open(tmp_path)
    first = runner.apply_pending(conn)
    second = runner.apply_pending(conn)
    assert first == ["v001_initial"]
    assert second == []


def test_reapply_does_not_duplicate_bookkeeping_rows(tmp_path):
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    runner.apply_pending(conn)
    runner.apply_pending(conn)
    rows = conn.execute(
        "SELECT id, COUNT(*) FROM applied_migrations GROUP BY id"
    ).fetchall()
    assert rows == [("v001_initial", 1)]


def test_reapply_does_not_recreate_tables(tmp_path):
    """If the runner accidentally re-ran v001 it would fail on
    ``CREATE TABLE schedules`` (no IF NOT EXISTS in v001). The
    no-op property is therefore enforced by both the bookkeeping
    check AND the CREATE TABLE statements themselves."""
    conn = _open(tmp_path)
    runner.apply_pending(conn)
    conn.execute(
        "INSERT INTO execution_plans (hash, body_json, enforcement, authored_at, author) "
        "VALUES ('h', '{}', 'strict', '2026-05-15T00:00:00+00:00', 'me')"
    )
    runner.apply_pending(conn)
    # Inserted row survives.
    rows = conn.execute("SELECT hash FROM execution_plans").fetchall()
    assert rows == [("h",)]


# ---------------------------------------------------------------------------
# Atomic-on-failure
# ---------------------------------------------------------------------------


class _PartialThenFailMigration:
    """Creates one user table, then raises. Used to verify that
    the runner's BEGIN ... ROLLBACK envelope undoes the partial
    work."""

    id = "v999_partial_fail"
    description = "test-only: partial CREATE then raise"

    def apply(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE partial_table (id TEXT PRIMARY KEY)"
        )
        raise RuntimeError("simulated mid-apply failure")


def test_mid_apply_failure_rolls_back_partial_tables(tmp_path):
    """After the migration's ``apply`` raises, the partial table
    it created must be gone — the per-migration TX wraps both."""
    conn = _open(tmp_path)
    with pytest.raises(RuntimeError, match="simulated mid-apply"):
        runner.apply_pending(conn, [_PartialThenFailMigration()])

    assert "partial_table" not in _table_names(conn)


def test_mid_apply_failure_does_not_record_bookkeeping(tmp_path):
    """A failed migration must NOT appear in
    ``applied_migrations`` — otherwise the next run would
    silently skip it."""
    conn = _open(tmp_path)
    with pytest.raises(RuntimeError):
        runner.apply_pending(conn, [_PartialThenFailMigration()])

    assert runner.applied_ids(conn) == set()


def test_mid_apply_failure_does_not_break_later_runs(tmp_path):
    """Verify the rollback leaves the DB clean enough for the
    real v001 migration to apply afterwards."""
    conn = _open(tmp_path)
    with pytest.raises(RuntimeError):
        runner.apply_pending(conn, [_PartialThenFailMigration()])
    # Now apply the real v001 — should succeed.
    newly = runner.apply_pending(conn, [V001Initial()])
    assert newly == ["v001_initial"]
    assert "schedules" in _table_names(conn)


class _SelfInsertingMigration:
    """Creates a table then inserts the runner's own bookkeeping
    row mid-apply, collision-bombing the runner's subsequent
    bookkeeping INSERT. Exercises the failure path where the
    apply body succeeds but the INSERT does not."""

    id = "v999_collide"
    description = "test-only: pre-empt the runner's bookkeeping INSERT"

    def apply(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE will_be_rolled_back (id TEXT)")
        conn.execute(
            "INSERT INTO applied_migrations (id, applied_at) "
            "VALUES ('v999_collide', '2026-05-15T00:00:00+00:00')"
        )


def test_bookkeeping_insert_failure_rolls_back_tables(tmp_path):
    """When the runner's bookkeeping INSERT raises (PK conflict),
    the per-migration TX rolls back the migration's CREATE
    statements too — no half-applied state."""
    conn = _open(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        runner.apply_pending(conn, [_SelfInsertingMigration()])
    assert "will_be_rolled_back" not in _table_names(conn)
    # And no orphaned bookkeeping row survives.
    assert runner.applied_ids(conn) == set()


# ---------------------------------------------------------------------------
# Bookkeeping table is created before any business table
# ---------------------------------------------------------------------------


class _CapturingMigration:
    """Records which tables existed at the moment its ``apply``
    runs. Lets us assert ordering."""

    id = "v999_capture"
    description = "test-only: capture table set at apply time"

    def __init__(self) -> None:
        self.tables_seen: set[str] = set()

    def apply(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        self.tables_seen = {row[0] for row in rows}


def test_bookkeeping_table_exists_before_migration_applies(tmp_path):
    conn = _open(tmp_path)
    spy = _CapturingMigration()
    runner.apply_pending(conn, [spy])
    assert "applied_migrations" in spy.tables_seen


# ---------------------------------------------------------------------------
# Caller-passed connection is honored (no implicit DB path)
# ---------------------------------------------------------------------------


def test_runner_does_not_open_production_db(tmp_path, monkeypatch):
    """A guard against accidental production wiring in phase 1.
    The runner should never call ``sqlite3.connect`` itself; the
    caller always passes a connection. We assert that by mocking
    ``sqlite3.connect`` so any internal call would surface."""
    calls: List[tuple] = []
    real_connect = sqlite3.connect

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    conn = real_connect(str(tmp_path / "test.db"))
    calls.clear()
    runner.apply_pending(conn)
    assert calls == []
