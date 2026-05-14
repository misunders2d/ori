"""Tests for ``app.v2.storage.schedule_state``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.8:

- First write at ``expected_version=0`` inserts version 1.
- CAS with stale version returns False, no mutation.
- CAS with current version returns True, bumps version.
- ``get_state`` returns None when absent, ScheduleState
  when present.
- ``written_by_run`` accepts None (author-time seed) AND a
  real run id; FK rejects a ghost run id.

Plus:
- Round-trip values for dict / list / scalar / None / nested.
- ``now`` tz-aware enforced; non-UTC normalised to UTC.
- ``expected_version < 0`` rejected with ValueError.
- No retry loop inside the helper (CAS returns False, caller
  retries).
- Helper rejects unprepared connection.

Smoke:
- No I/O imports.
- No execution / claim-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import FireReason, RunStatus
from app.v2.migrations import runner
from app.v2.storage import schedule_state as schedule_state_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.schedule_state import (
    StateRunMismatchError,
    get_state,
    set_state_cas,
)
from app.v2.storage.serialization import NaiveDatetimeError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(
    conn: sqlite3.Connection, schedule_id: str = "daily_audit"
) -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            "{}",
            "test",
            "{}",
            "{}",
            "{}",
            "{}",
            "active",
            _NOW.isoformat(),
            f"hash-{schedule_id}",
        ),
    )


def _seed_run(conn: sqlite3.Connection, run_id: str = "run-abc") -> None:
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "daily_audit",
            "scheduled",
            _NOW.isoformat(),
            "pending",
            1,
            run_id,
        ),
    )


# ===========================================================================
# First write
# ===========================================================================


def test_first_write_inserts_with_version_one(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="last_fired_day",
        new_value=3,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    assert ok is True
    fetched = get_state(conn, schedule_id="daily_audit", key="last_fired_day")
    assert fetched.version == 1
    assert fetched.value == 3
    assert fetched.written_by_run is None


def test_first_write_when_row_already_exists_returns_false(tmp_path):
    """``expected_version=0`` against a row that already exists
    (version >= 1) is a stale write — INSERT OR IGNORE returns
    rowcount=0, helper returns False, no mutation."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=42,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    racy = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=99,
        expected_version=0,
        written_by_run=None,
        now=_NOW + timedelta(seconds=1),
    )
    assert racy is False
    # Original value preserved.
    fetched = get_state(conn, schedule_id="daily_audit", key="counter")
    assert fetched.value == 42
    assert fetched.version == 1


# ===========================================================================
# CAS update
# ===========================================================================


def test_cas_with_current_version_succeeds(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=2,
        expected_version=1,
        written_by_run=None,
        now=_NOW + timedelta(seconds=1),
    )
    assert ok is True
    fetched = get_state(conn, schedule_id="daily_audit", key="counter")
    assert fetched.value == 2
    assert fetched.version == 2


def test_cas_with_stale_version_returns_false(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # Seed v1.
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=10,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    # Bump to v2.
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=20,
        expected_version=1,
        written_by_run=None,
        now=_NOW + timedelta(seconds=1),
    )
    # Try CAS at the stale v1.
    stale = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=999,
        expected_version=1,
        written_by_run=None,
        now=_NOW + timedelta(seconds=2),
    )
    assert stale is False
    # Stored value still v2's.
    fetched = get_state(conn, schedule_id="daily_audit", key="counter")
    assert fetched.value == 20
    assert fetched.version == 2


def test_cas_update_with_no_existing_row_returns_false(tmp_path):
    """``expected_version=1`` on a key that has no row → no
    match → helper returns False. Caller observes the False
    and switches to a ``expected_version=0`` first-write retry."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="never_written",
        new_value="x",
        expected_version=1,
        written_by_run=None,
        now=_NOW,
    )
    assert ok is False
    assert (
        get_state(conn, schedule_id="daily_audit", key="never_written") is None
    )


def test_cas_no_internal_retry_loop(tmp_path):
    """Helper returns False on stale; caller drives the retry.
    Calling once with stale + once with current shows the
    semantics — no loop inside."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )

    # First CAS at stale version (already-v1, caller passes 0).
    stale = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=2,
        expected_version=0,
        written_by_run=None,
        now=_NOW + timedelta(seconds=1),
    )
    assert stale is False  # not retried

    # Caller reads fresh state and retries with current version.
    fresh = get_state(conn, schedule_id="daily_audit", key="k")
    retry = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=2,
        expected_version=fresh.version,
        written_by_run=None,
        now=_NOW + timedelta(seconds=2),
    )
    assert retry is True


# ===========================================================================
# get_state
# ===========================================================================


def test_get_state_returns_none_when_absent(tmp_path):
    conn = _migrate(tmp_path)
    assert get_state(conn, schedule_id="any", key="any") is None


def test_get_state_returns_schedule_state_when_present(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="last_fired_day",
        new_value=5,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    fetched = get_state(
        conn, schedule_id="daily_audit", key="last_fired_day"
    )
    assert fetched.schedule_id == "daily_audit"
    assert fetched.key == "last_fired_day"
    assert fetched.value == 5
    assert fetched.version == 1


# ===========================================================================
# Value type coverage
# ===========================================================================


@pytest.mark.parametrize(
    "value",
    [
        42,
        3.14,
        "hello",
        True,
        False,
        None,
        [1, 2, 3],
        {"k": "v", "n": 7},
        [{"item": 1}, {"item": 2}],
        {"nested": {"deep": ["x", "y"]}},
    ],
)
def test_value_round_trip(tmp_path, value):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=value,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.value == value


# ===========================================================================
# written_by_run handling
# ===========================================================================


def test_written_by_run_accepts_none(tmp_path):
    """Author-time seeds have no run id to attribute. NULL is
    a legitimate written_by_run."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.written_by_run is None


def test_written_by_run_accepts_existing_run(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="run-abc")
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run="run-abc",
        now=_NOW,
    )
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.written_by_run == "run-abc"


def test_written_by_run_rejects_missing_run(tmp_path):
    """FK on schedule_state.written_by_run → runs.id. A ghost
    run id raises IntegrityError at INSERT time."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    with pytest.raises(sqlite3.IntegrityError):
        set_state_cas(
            conn,
            schedule_id="daily_audit",
            key="k",
            new_value=1,
            expected_version=0,
            written_by_run="ghost-run",
            now=_NOW,
        )


# ===========================================================================
# Cross-schedule written_by_run guard (reviewer follow-up).
# Same audit-truth corruption pattern as events.append_event +
# transactions.update_run_status_and_append_event. State row
# attributes a lineage entry to a run from a different
# schedule? Refuse.
# ===========================================================================


def test_first_write_rejects_cross_schedule_run(tmp_path):
    """Two schedules; run lives under schedule A. State row for
    schedule B that attributes write to run-A must be rejected
    with StateRunMismatchError BEFORE the INSERT runs. No row
    leaks to either schedule."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "weekly_report")
    _seed_run(conn, run_id="run-from-daily")

    with pytest.raises(StateRunMismatchError, match="written_by_run"):
        set_state_cas(
            conn,
            schedule_id="weekly_report",  # state belongs here
            key="k",
            new_value=1,
            expected_version=0,
            written_by_run="run-from-daily",  # but run is daily_audit's
            now=_NOW,
        )

    # Neither schedule has a state row.
    assert get_state(conn, schedule_id="daily_audit", key="k") is None
    assert get_state(conn, schedule_id="weekly_report", key="k") is None


def test_cas_update_rejects_cross_schedule_run(tmp_path):
    """Existing state row + CAS update with a run from a
    different schedule → reject. Old row untouched."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "weekly_report")
    _seed_run(conn, run_id="run-daily")
    # Seed the state row under daily_audit via author-time seed.
    set_state_cas(
        conn,
        schedule_id="weekly_report",
        key="counter",
        new_value=5,
        expected_version=0,
        written_by_run=None,  # author-time seed
        now=_NOW,
    )

    # CAS update that attempts to attribute the bump to a run
    # belonging to a different schedule.
    with pytest.raises(StateRunMismatchError, match="written_by_run"):
        set_state_cas(
            conn,
            schedule_id="weekly_report",
            key="counter",
            new_value=99,
            expected_version=1,
            written_by_run="run-daily",  # belongs to daily_audit, not weekly_report
            now=_NOW + timedelta(seconds=1),
        )

    # Original row unchanged.
    fetched = get_state(
        conn, schedule_id="weekly_report", key="counter"
    )
    assert fetched.value == 5
    assert fetched.version == 1
    assert fetched.written_by_run is None


def test_first_write_accepts_matching_schedule_run(tmp_path):
    """Positive control: run belongs to the same schedule as
    the state row → write succeeds."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="run-abc")
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run="run-abc",
        now=_NOW,
    )
    assert ok is True
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.written_by_run == "run-abc"


def test_cas_update_accepts_matching_schedule_run(tmp_path):
    """Positive control for the CAS path: matching schedule
    succeeds after a fresh-read retry."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="run-abc")
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=2,
        expected_version=1,
        written_by_run="run-abc",
        now=_NOW + timedelta(seconds=1),
    )
    assert ok is True
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.written_by_run == "run-abc"
    assert fetched.value == 2


def test_written_by_run_none_still_succeeds_after_guard(tmp_path):
    """The cross-check only runs when written_by_run is not
    None. Pinning that None continues to work (author-time
    seeds are common)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    ok = set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    assert ok is True


def test_cas_update_with_changed_written_by_run(tmp_path):
    """Successful CAS-update replaces written_by_run alongside
    value. Pin the field gets updated, not just value/version."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="run-1")
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=2,
        expected_version=1,
        written_by_run="run-1",
        now=_NOW + timedelta(seconds=1),
    )
    fetched = get_state(conn, schedule_id="daily_audit", key="k")
    assert fetched.written_by_run == "run-1"


# ===========================================================================
# Datetime + UTC normalisation
# ===========================================================================


def test_rejects_naive_now(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(NaiveDatetimeError, match="now"):
        set_state_cas(
            conn,
            schedule_id="daily_audit",
            key="k",
            new_value=1,
            expected_version=0,
            written_by_run=None,
            now=naive,
        )
    assert get_state(conn, schedule_id="daily_audit", key="k") is None


def test_normalises_non_utc_now_to_utc(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    eastern = datetime(
        2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="k",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=eastern,
    )
    stored = conn.execute(
        "SELECT written_at FROM schedule_state WHERE key = 'k'"
    ).fetchone()[0]
    assert stored.endswith("+00:00")


# ===========================================================================
# Argument validation
# ===========================================================================


@pytest.mark.parametrize("bad_version", [-1, -100])
def test_expected_version_rejects_negative(tmp_path, bad_version):
    conn = _migrate(tmp_path)
    with pytest.raises(ValueError, match="expected_version"):
        set_state_cas(
            conn,
            schedule_id="daily_audit",
            key="k",
            new_value=1,
            expected_version=bad_version,
            written_by_run=None,
            now=_NOW,
        )


# ===========================================================================
# Isolation
# ===========================================================================


def test_state_isolated_per_schedule_and_key(tmp_path):
    """``(schedule_id, key)`` is the composite PK — different
    schedules with the same key are independent rows."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "weekly_report")
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="counter",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    set_state_cas(
        conn,
        schedule_id="weekly_report",
        key="counter",
        new_value=100,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    assert (
        get_state(conn, schedule_id="daily_audit", key="counter").value == 1
    )
    assert (
        get_state(conn, schedule_id="weekly_report", key="counter").value
        == 100
    )


def test_state_isolated_per_key(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="a",
        new_value=1,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    set_state_cas(
        conn,
        schedule_id="daily_audit",
        key="b",
        new_value=2,
        expected_version=0,
        written_by_run=None,
        now=_NOW,
    )
    assert get_state(conn, schedule_id="daily_audit", key="a").value == 1
    assert get_state(conn, schedule_id="daily_audit", key="b").value == 2


# ===========================================================================
# Connection guard
# ===========================================================================


def test_get_state_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_state(bare, schedule_id="x", key="y")


def test_set_state_cas_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        set_state_cas(
            bare,
            schedule_id="x",
            key="y",
            new_value=1,
            expected_version=0,
            written_by_run=None,
            now=_NOW,
        )


# ===========================================================================
# Smoke
# ===========================================================================


def test_schedule_state_module_has_no_io_imports():
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
    for _, member in vars(schedule_state_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"schedule_state module imports I/O libs: {sorted(leaked)}."
    )


def test_schedule_state_module_has_no_dispatch_callables():
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
        "claim",
        "claim_run",
    }
    for name, member in vars(schedule_state_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"schedule_state module exposes execution / claim "
                f"suggestive callable: {name}"
            )
