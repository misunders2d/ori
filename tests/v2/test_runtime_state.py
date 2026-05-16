"""Phase 13 slice 1 — §6.5 cross-fire state RUNTIME primitives.

Per ``docs/PHASE_13_PLAN.md`` §1 / §3 / §9 (Q1–Q6) + the
claude-reviewer slice-1 hard-checks:

- Thin typed DI-conn layer over the SHIPPED phase-3
  ``get_state`` / ``set_state_cas`` — no SQL / no DDL / storage
  byte-untouched.
- ``state_write`` typed ``written`` / ``stale_version``; NO
  internal retry/spin (racing writers: the loser gets
  ``stale_version`` from a SINGLE call, primitive does not
  spin); ``StateRunMismatchError`` PROPAGATES;
  ``NaiveDatetimeError`` intact.
- ``state_read`` → ``{value, version, written_at,
  written_by_run}`` or ``None``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.v2.migrations import runner
from app.v2.runtime.state import (
    StateView,
    StateWriteOutcome,
    state_read,
    state_write,
)
from app.v2.storage.schedule_state import StateRunMismatchError
from app.v2.storage.serialization import NaiveDatetimeError

_NOW = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(conn: sqlite3.Connection, sid: str = "daily_audit") -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, "{}", "test", "{}", "{}", "{}", "{}", "active",
         _NOW.isoformat(), f"hash-{sid}"),
    )


def _seed_run(
    conn: sqlite3.Connection,
    run_id: str = "run-abc",
    schedule_id: str = "daily_audit",
) -> None:
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, schedule_id, "scheduled", _NOW.isoformat(),
         "pending", 1, run_id),
    )


def _w(conn, **kw):
    base = dict(
        schedule_id="daily_audit",
        key="cursor",
        value={"i": 1},
        written_by_run="run-abc",
        now=_NOW,
    )
    base.update(kw)
    return state_write(conn, **base)


# ---------------------------------------------------------------------------
# First write / read shape
# ---------------------------------------------------------------------------


def test_first_write_none_expected_is_written_version_1(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    out = _w(conn, expected_version=None)
    assert out == StateWriteOutcome(status="written", version=1)
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert isinstance(view, StateView)
    assert view.value == {"i": 1}
    assert view.version == 1
    assert view.written_by_run == "run-abc"
    assert view.written_at is not None


def test_first_write_explicit_zero_same_as_none(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    assert _w(conn, expected_version=0) == StateWriteOutcome(
        status="written", version=1
    )


def test_state_read_absent_is_none(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    assert (
        state_read(conn, schedule_id="daily_audit", key="nope")
        is None
    )


def test_author_time_seed_written_by_run_none(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _w(conn, written_by_run=None, expected_version=None)
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert view.written_by_run is None


# ---------------------------------------------------------------------------
# First-write collision (no unconditional overwrite, no TOCTOU)
# ---------------------------------------------------------------------------


def test_first_write_collision_is_stale_no_mutation(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    _w(conn, value={"i": 1}, expected_version=None)
    out = _w(conn, value={"i": 999}, expected_version=None)
    assert out == StateWriteOutcome(status="stale_version", version=None)
    # Unchanged — the second first-write did NOT overwrite.
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert view.value == {"i": 1}
    assert view.version == 1


# ---------------------------------------------------------------------------
# CAS update
# ---------------------------------------------------------------------------


def test_cas_correct_version_bumps(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    _w(conn, value={"i": 1}, expected_version=None)  # v1
    out = _w(conn, value={"i": 2}, expected_version=1)
    assert out == StateWriteOutcome(status="written", version=2)
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert view.value == {"i": 2}
    assert view.version == 2


def test_cas_stale_version_refused_no_mutation(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    _w(conn, value={"i": 1}, expected_version=None)  # v1
    out = _w(conn, value={"i": 9}, expected_version=5)  # wrong
    assert out == StateWriteOutcome(status="stale_version", version=None)
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert view.value == {"i": 1}
    assert view.version == 1


def test_racing_writers_loser_gets_stale_no_spin(tmp_path):
    """Two writers both read v1 and both CAS with
    expected_version=1. First wins → v2. Second's SINGLE call
    returns stale_version immediately — the primitive does NOT
    spin/retry (caller owns retry)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    _w(conn, value={"i": 1}, expected_version=None)  # v1
    first = _w(conn, value={"i": 2}, expected_version=1)
    second = _w(conn, value={"i": 3}, expected_version=1)
    assert first == StateWriteOutcome(status="written", version=2)
    assert second == StateWriteOutcome(
        status="stale_version", version=None
    )
    view = state_read(conn, schedule_id="daily_audit", key="cursor")
    assert view.value == {"i": 2}  # first writer's value stuck
    assert view.version == 2


# ---------------------------------------------------------------------------
# Propagation: lineage / naive datetime / negative version
# ---------------------------------------------------------------------------


def test_state_run_mismatch_propagates(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "other_sched")
    _seed_run(conn, "run-other", "other_sched")
    with pytest.raises(StateRunMismatchError):
        state_write(
            conn,
            schedule_id="daily_audit",
            key="cursor",
            value={"i": 1},
            written_by_run="run-other",  # belongs to other_sched
            now=_NOW,
            expected_version=None,
        )


def test_naive_datetime_propagates(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    with pytest.raises(NaiveDatetimeError):
        _w(conn, now=datetime(2026, 5, 16, 9, 0))  # naive


def test_negative_expected_version_propagates_valueerror(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    with pytest.raises(ValueError):
        _w(conn, expected_version=-1)


# ---------------------------------------------------------------------------
# Frozen typed surfaces
# ---------------------------------------------------------------------------


def test_outcomes_are_frozen():
    o = StateWriteOutcome(status="written", version=1)
    with pytest.raises(Exception):
        o.status = "stale_version"  # type: ignore[misc]
    v = StateView(
        value=1, version=1, written_at=_NOW, written_by_run=None
    )
    with pytest.raises(Exception):
        v.version = 2  # type: ignore[misc]
