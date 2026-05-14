"""Tests for the SQLite CHECK / NOT NULL / FOREIGN KEY
constraints declared in ``app/v2/ddl/v001_initial.sql``.

These tests run against a fresh DB that has had
``apply_pending`` applied. They poke each declared constraint
with an invalid INSERT and assert ``IntegrityError`` — the schema
is the last line of defense if a future model regression lets a
bad value through Pydantic.

Pins per ``docs/PHASE_1_PLAN.md`` §5.3:
- ``runs.status`` CHECK rejects unknown values (including
  ``retry_pending``, which was deliberately dropped from
  RunStatus in the round-7 decision).
- ``runs.fire_reason`` CHECK rejects unknown values.
- ``schedules.status`` CHECK rejects unknown values.
- ``runs.root_run_id`` NOT NULL rejects nulls.
- Foreign keys enforced when ``PRAGMA foreign_keys=ON``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.v2.migrations import runner


_NOW = "2026-05-15T09:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixture: a fresh migrated DB, ready to receive INSERTs.
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


# ---------------------------------------------------------------------------
# Insert helpers — build a minimum valid schedule + run we can
# then mutate per-test.
# ---------------------------------------------------------------------------


def _insert_schedule(
    conn: sqlite3.Connection,
    *,
    schedule_id: str = "daily_audit",
    status: str = "active",
    hash_: str = "h-daily-1",
) -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, failure_json, "
        " audit_json, status, execution_plan_hash, template_json, "
        " authored_at, parent_hash, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            "sergey@mellanni.com",
            "test schedule",
            "{}",
            "{}",
            "{}",
            "{}",
            status,
            None,
            None,
            _NOW,
            None,
            hash_,
        ),
    )


def _insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    schedule_id: str = "daily_audit",
    fire_reason: str = "scheduled",
    status: str = "pending",
    attempt: int = 1,
    root_run_id: str | None = None,
    parent_run_id: str | None = None,
) -> None:
    if root_run_id is None:
        root_run_id = run_id  # first-attempt self-ref
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, execution_plan_hash, fire_reason, due_at, "
        " status, attempt, root_run_id, parent_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            schedule_id,
            None,
            fire_reason,
            _NOW,
            status,
            attempt,
            root_run_id,
            parent_run_id,
        ),
    )


# ---------------------------------------------------------------------------
# schedules.status CHECK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "paused", "archived"])
def test_schedules_status_accepts_canonical_values(migrated_conn, status):
    _insert_schedule(migrated_conn, status=status, hash_=f"h-{status}")


@pytest.mark.parametrize("status", ["enabled", "disabled", "running", ""])
def test_schedules_status_rejects_unknown_values(migrated_conn, status):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_schedule(migrated_conn, status=status, hash_=f"h-bad-{status}")


# ---------------------------------------------------------------------------
# execution_plans.enforcement CHECK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enforcement", ["strict", "permissive"])
def test_execution_plans_enforcement_accepts_canonical(migrated_conn, enforcement):
    migrated_conn.execute(
        "INSERT INTO execution_plans "
        "(hash, body_json, enforcement, authored_at, author) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"plan-{enforcement}", "{}", enforcement, _NOW, "sergey"),
    )


@pytest.mark.parametrize("enforcement", ["lax", "off", ""])
def test_execution_plans_enforcement_rejects_unknown(migrated_conn, enforcement):
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO execution_plans "
            "(hash, body_json, enforcement, authored_at, author) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"plan-bad-{enforcement}", "{}", enforcement, _NOW, "sergey"),
        )


# ---------------------------------------------------------------------------
# runs.status CHECK — including the deliberately-dropped value
# 'retry_pending'.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["pending", "claimed", "running", "succeeded", "failed", "cancelled"],
)
def test_runs_status_accepts_canonical_values(migrated_conn, status):
    _insert_schedule(migrated_conn)
    _insert_run(migrated_conn, run_id=f"r-{status}", status=status)


@pytest.mark.parametrize(
    "status",
    ["retry_pending", "waiting", "queued", "done", ""],
)
def test_runs_status_rejects_unknown_values(migrated_conn, status):
    """Retry-chain semantics (D-round-6) forbid ``retry_pending``
    — a retry is a NEW pending Run, not a re-status of the old
    one. The CHECK constraint is the schema-level enforcement of
    that decision."""
    _insert_schedule(migrated_conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(migrated_conn, run_id=f"r-bad-{status}", status=status)


# ---------------------------------------------------------------------------
# runs.fire_reason CHECK
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["scheduled", "manual", "retry", "replay", "backfill"]
)
def test_runs_fire_reason_accepts_canonical_values(migrated_conn, reason):
    _insert_schedule(migrated_conn)
    _insert_run(migrated_conn, run_id=f"r-{reason}", fire_reason=reason)


@pytest.mark.parametrize("reason", ["forced", "auto", "user", ""])
def test_runs_fire_reason_rejects_unknown_values(migrated_conn, reason):
    _insert_schedule(migrated_conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(migrated_conn, run_id=f"r-bad-{reason}", fire_reason=reason)


# ---------------------------------------------------------------------------
# runs.root_run_id NOT NULL — the round-6 retry-chain invariant.
# ---------------------------------------------------------------------------


def test_runs_root_run_id_not_null(migrated_conn):
    _insert_schedule(migrated_conn)
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO runs "
            "(id, schedule_id, fire_reason, due_at, status, attempt, "
            " root_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r-orphan", "daily_audit", "scheduled", _NOW, "pending", 1, None),
        )


# ---------------------------------------------------------------------------
# Foreign keys enforced when foreign_keys=ON
# ---------------------------------------------------------------------------


def test_runs_schedule_fk_enforced(migrated_conn):
    """The runner sets ``PRAGMA foreign_keys=ON``. An INSERT into
    ``runs`` with an unknown ``schedule_id`` must raise."""
    # No matching schedule was inserted.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_run(migrated_conn, run_id="r-no-sched", schedule_id="ghost_schedule")


def test_events_run_fk_enforced(migrated_conn):
    _insert_schedule(migrated_conn)
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO events "
            "(id, run_id, schedule_id, ts, kind, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e1", "missing_run", "daily_audit", _NOW, "run_created", "{}"),
        )


def test_schedule_state_fk_enforced(migrated_conn):
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO schedule_state "
            "(schedule_id, key, value_json, version, written_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ghost_schedule", "last_fired", "1", 1, _NOW),
        )


def test_source_snapshots_run_fk_enforced(migrated_conn):
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO source_snapshots "
            "(run_id, source_id, content_hash, content_path, content_size, "
            " fetched_at, source_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "missing_run",
                "syllabus",
                "sha256:" + "a" * 64,
                "data/x.json",
                10,
                _NOW,
                "source_drive_file",
            ),
        )


# ---------------------------------------------------------------------------
# Schedule hash UNIQUE
# ---------------------------------------------------------------------------


def test_schedules_hash_unique(migrated_conn):
    _insert_schedule(migrated_conn, schedule_id="a", hash_="dup_hash")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_schedule(migrated_conn, schedule_id="b", hash_="dup_hash")


# ---------------------------------------------------------------------------
# Indexes present (smoke check — partial indexes can silently
# not get created if SQLite version is too old).
# ---------------------------------------------------------------------------


def test_partial_indexes_created(migrated_conn):
    """``idx_runs_pending_due`` and ``idx_runs_schedule_running``
    are partial indexes. They're the workhorse indexes for the
    worker pool's pending-run scan and the single-flight check.
    Verify they exist in ``sqlite_master``."""
    rows = migrated_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    index_names = {row[0] for row in rows}
    assert "idx_runs_pending_due" in index_names
    assert "idx_runs_schedule_running" in index_names
    assert "idx_runs_root" in index_names
