"""Tests for ``app.v2.runtime.claim``.

Pins per ``docs/PHASE_4_PLAN.md`` §4.3:

- Single pending + active schedule → claim returns True;
  status flips; claimed_by + claimed_at set; run_claimed
  event appended.
- Status mismatch (already claimed/running) → False, no
  event.
- Single-flight per schedule: same-schedule already
  claimed/running blocks claim; cross-schedule independent.
- **Paused schedule + pending run → False, run stays pending,
  no event leaks** (round-1 regression pin for the paused-
  schedule claim gap).
- Archived schedule → False, no event.
- Schedule transitions paused → active mid-test → claimable.
- Naive now rejected.
- Atomicity: pre-seeded duplicate event_id makes the INSERT
  fail; the claim UPDATE rolls back.
- Non-UTC now stored as +00:00 in both claimed_at and event
  ts.
- event_id is injected — no uuid module imports in claim.py.
- assert_connection_ready called.

Smoke:
- No I/O imports.
- No reasoning/emit/delegate/transfer callables — phase-4
  worker body is empty, runtime modules don't dispatch.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import EventKind, RunStatus, ScheduleStatus
from app.v2.migrations import runner
from app.v2.runtime import claim as claim_mod
from app.v2.runtime.claim import claim_run
from app.v2.runtime.state_machine import IllegalTransitionError
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.serialization import NaiveDatetimeError


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(
    conn: sqlite3.Connection,
    schedule_id: str = "daily_audit",
    status: str = "active",
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
            status,
            _NOW.isoformat(),
            f"hash-{schedule_id}",
        ),
    )


def _seed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run-abc",
    schedule_id: str = "daily_audit",
    status: str = "pending",
) -> None:
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            schedule_id,
            "scheduled",
            _NOW.isoformat(),
            status,
            1,
            run_id,
        ),
    )


def _status(conn: sqlite3.Connection, run_id: str) -> str:
    return conn.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0]


def _event_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


# ===========================================================================
# Happy path
# ===========================================================================


def test_claim_succeeds_with_pending_run_and_active_schedule(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    ok = claim_run(
        conn,
        "run-abc",
        claimed_by="worker-1",
        now=_NOW,
        event_id="evt-claim-1",
    )
    assert ok is True
    assert _status(conn, "run-abc") == "claimed"

    row = conn.execute(
        "SELECT claimed_by, claimed_at FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "worker-1"
    assert row[1] == _NOW.isoformat()


def test_claim_writes_paired_run_claimed_event(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    claim_run(
        conn,
        "run-abc",
        claimed_by="worker-1",
        now=_NOW,
        event_id="evt-claim-1",
    )
    row = conn.execute(
        "SELECT run_id, schedule_id, kind, payload_json, correlates "
        "FROM events WHERE id = 'evt-claim-1'"
    ).fetchone()
    assert row is not None
    run_id, schedule_id, kind, payload_json, correlates = row
    assert run_id == "run-abc"
    assert schedule_id == "daily_audit"
    assert kind == EventKind.RUN_CLAIMED.value
    assert '"claimed_by":"worker-1"' in payload_json
    assert correlates is None


# ===========================================================================
# Negative paths
# ===========================================================================


def test_claim_fails_on_already_claimed_run(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="claimed")

    ok = claim_run(
        conn,
        "run-abc",
        claimed_by="worker-2",
        now=_NOW,
        event_id="evt-x",
    )
    assert ok is False
    # No new event row.
    assert _event_count(conn) == 0


def test_claim_fails_on_running_run(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="running")

    ok = claim_run(
        conn,
        "run-abc",
        claimed_by="w",
        now=_NOW,
        event_id="evt-x",
    )
    assert ok is False
    assert _event_count(conn) == 0


def test_claim_fails_on_succeeded_run(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="succeeded")

    ok = claim_run(
        conn,
        "run-abc",
        claimed_by="w",
        now=_NOW,
        event_id="evt-x",
    )
    assert ok is False
    assert _event_count(conn) == 0


def test_claim_fails_for_missing_run_id(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)

    ok = claim_run(
        conn,
        "ghost-run",
        claimed_by="w",
        now=_NOW,
        event_id="evt-x",
    )
    assert ok is False
    assert _event_count(conn) == 0


# ===========================================================================
# Single-flight
# ===========================================================================


def test_two_pending_on_different_schedules_both_claim(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "sched_a")
    _seed_schedule(conn, "sched_b")
    _seed_run(conn, run_id="r-a", schedule_id="sched_a")
    _seed_run(conn, run_id="r-b", schedule_id="sched_b")

    ok_a = claim_run(
        conn, "r-a", claimed_by="w", now=_NOW, event_id="evt-a"
    )
    ok_b = claim_run(
        conn, "r-b", claimed_by="w", now=_NOW, event_id="evt-b"
    )
    assert ok_a is True
    assert ok_b is True
    assert _status(conn, "r-a") == "claimed"
    assert _status(conn, "r-b") == "claimed"


def test_two_pending_same_schedule_only_one_claims(tmp_path):
    """Single-flight: with two pending runs on the same
    schedule and one already claimed, the second cannot
    claim."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r-1")
    _seed_run(conn, run_id="r-2")

    first = claim_run(
        conn, "r-1", claimed_by="w", now=_NOW, event_id="evt-1"
    )
    assert first is True
    second = claim_run(
        conn, "r-2", claimed_by="w", now=_NOW, event_id="evt-2"
    )
    assert second is False
    assert _status(conn, "r-2") == "pending"
    # Only the first claim's event landed.
    assert _event_count(conn) == 1


def test_pending_blocked_by_running_on_same_schedule(tmp_path):
    """A claim attempt for a pending run on a schedule with a
    running run must fail (single-flight predicate)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r-running", status="running")
    _seed_run(conn, run_id="r-pending", status="pending")

    ok = claim_run(
        conn, "r-pending", claimed_by="w", now=_NOW, event_id="evt-x"
    )
    assert ok is False
    assert _status(conn, "r-pending") == "pending"


def test_single_pending_only_row_on_schedule_claims(tmp_path):
    """Behavioural pin: when the target row is the ONLY row on
    its schedule, the single-flight NOT EXISTS predicate must
    not block the claim. This test alone does NOT prove the
    ``r2.id != runs.id`` clause is present — current SQLite
    evaluates the target row as pre-update ``pending`` so a
    regression that dropped the clause would still pass here.
    The structural test below pins the clause itself."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="solo-run")
    # Sanity: no other rows on the schedule.
    assert conn.execute(
        "SELECT COUNT(*) FROM runs WHERE schedule_id = 'daily_audit'"
    ).fetchone()[0] == 1

    ok = claim_run(
        conn, "solo-run", claimed_by="w", now=_NOW, event_id="evt-solo"
    )
    assert ok is True
    assert _status(conn, "solo-run") == "claimed"


def test_claim_sql_self_exclusion_clause_present():
    """Structural pin for ``r2.id != runs.id`` in the
    single-flight subquery. The behavioural happy-path test
    cannot distinguish "clause present" from "clause absent
    but SQLite happens to read pre-update state" — both would
    pass today. This test reads the module source and asserts
    the clause literally exists, so a regression that removes
    it fails here with a clear, focused message rather than
    silently relying on SQLite's WHERE-evaluation ordering."""
    source = inspect.getsource(claim_mod)
    assert "r2.id != runs.id" in source, (
        "claim_run's single-flight predicate must explicitly "
        "exclude the target row via 'r2.id != runs.id'. Without "
        "it the predicate's correctness depends on SQLite's "
        "UPDATE-WHERE evaluation order (target row read as "
        "pre-update), which is brittle to triggers, future "
        "SQLite versions, and any UPDATE-FROM rewrites."
    )


def test_cross_schedule_no_interference(tmp_path):
    """A claimed run on schedule A must NOT block claiming
    a pending run on schedule B (single-flight is
    per-schedule)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "sched_a")
    _seed_schedule(conn, "sched_b")
    _seed_run(conn, run_id="r-a", schedule_id="sched_a", status="claimed")
    _seed_run(conn, run_id="r-b", schedule_id="sched_b", status="pending")

    ok = claim_run(
        conn, "r-b", claimed_by="w", now=_NOW, event_id="evt-b"
    )
    assert ok is True
    assert _status(conn, "r-b") == "claimed"


# ===========================================================================
# Schedule-status predicate (round-1 regression pin).
# Pending rows that pre-dated a pause / archive MUST NOT
# claim. Eraser of the round-1 paused-schedule claim gap.
# ===========================================================================


def test_paused_schedule_pending_run_does_not_claim(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, status="paused")
    _seed_run(conn)

    ok = claim_run(
        conn, "run-abc", claimed_by="w", now=_NOW, event_id="evt-x"
    )
    assert ok is False
    assert _status(conn, "run-abc") == "pending"
    # No event leaked.
    assert _event_count(conn) == 0


def test_archived_schedule_pending_run_does_not_claim(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, status="archived")
    _seed_run(conn)

    ok = claim_run(
        conn, "run-abc", claimed_by="w", now=_NOW, event_id="evt-x"
    )
    assert ok is False
    assert _status(conn, "run-abc") == "pending"
    assert _event_count(conn) == 0


def test_paused_to_active_transition_unblocks_claim(tmp_path):
    """A pending run that was refused while the schedule was
    paused becomes claimable once the schedule flips back to
    active."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, status="paused")
    _seed_run(conn)

    refused = claim_run(
        conn, "run-abc", claimed_by="w", now=_NOW, event_id="evt-1"
    )
    assert refused is False

    # Flip schedule status to active.
    conn.execute(
        "UPDATE schedules SET status = 'active' WHERE id = 'daily_audit'"
    )

    ok = claim_run(
        conn, "run-abc", claimed_by="w", now=_NOW, event_id="evt-2"
    )
    assert ok is True
    assert _status(conn, "run-abc") == "claimed"


# ===========================================================================
# Datetime + UTC
# ===========================================================================


def test_claim_rejects_naive_now(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(NaiveDatetimeError, match="now"):
        claim_run(
            conn,
            "run-abc",
            claimed_by="w",
            now=naive,
            event_id="evt-x",
        )
    # Nothing changed.
    assert _status(conn, "run-abc") == "pending"
    assert _event_count(conn) == 0


def test_claim_normalises_non_utc_now_to_utc(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    eastern = datetime(
        2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    claim_run(
        conn,
        "run-abc",
        claimed_by="w",
        now=eastern,
        event_id="evt-x",
    )
    claimed_at = conn.execute(
        "SELECT claimed_at FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    event_ts = conn.execute(
        "SELECT ts FROM events WHERE id = 'evt-x'"
    ).fetchone()[0]
    assert claimed_at.endswith("+00:00")
    assert event_ts.endswith("+00:00")


# ===========================================================================
# Atomicity-on-failure
# ===========================================================================


def test_event_insert_failure_rolls_back_claim_update(tmp_path):
    """Pre-seed a row with the event_id the helper will try to
    use. The helper's INSERT fails with IntegrityError; the
    claim UPDATE rolls back; the run stays pending."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    # Need to satisfy events.run_id FK; insert under a
    # different run to avoid the cross-schedule guard in
    # append_event.
    _seed_run(conn, run_id="other-run")
    conn.execute(
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, payload_json, correlates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "evt-collide",
            "other-run",
            "daily_audit",
            _NOW.isoformat(),
            "run_created",
            "{}",
            None,
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        claim_run(
            conn,
            "run-abc",
            claimed_by="w",
            now=_NOW,
            event_id="evt-collide",
        )

    # Claim UPDATE rolled back: run still pending.
    assert _status(conn, "run-abc") == "pending"
    # Pre-existing event row survives (it was outside the TX);
    # only the new event row would have been added.
    assert _event_count(conn) == 1


# ===========================================================================
# State-machine policy gate
# ===========================================================================


def test_claim_consults_state_machine_before_sql(tmp_path, monkeypatch):
    """``claim_run`` must call ``assert_legal_transition(PENDING,
    CLAIMED)`` before touching SQL. Reviewer concern: if
    ``LEGAL_TRANSITIONS`` is ever narrowed in the future and this
    primitive does not consult the state machine, it would
    silently keep writing an illegal transition. We pin the
    dependency by monkey-patching the imported symbol to raise
    and asserting (a) the error propagates and (b) no row
    changed.

    The hardcoded endpoints in claim_run mirror the SQL
    predicate; both ends must move together if the table is
    ever narrowed."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    calls: list[tuple[RunStatus, RunStatus]] = []

    def _fake_assert(src, dst):
        calls.append((src, dst))
        raise IllegalTransitionError(f"forbidden: {src.value}→{dst.value}")

    monkeypatch.setattr(claim_mod, "assert_legal_transition", _fake_assert)

    with pytest.raises(IllegalTransitionError):
        claim_run(
            conn,
            "run-abc",
            claimed_by="w",
            now=_NOW,
            event_id="evt-x",
        )

    # The gate was called with the exact (PENDING, CLAIMED)
    # pair the SQL hardcodes.
    assert calls == [(RunStatus.PENDING, RunStatus.CLAIMED)]
    # Row untouched.
    assert _status(conn, "run-abc") == "pending"
    assert _event_count(conn) == 0


# ===========================================================================
# Connection guard
# ===========================================================================


def test_claim_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        claim_run(
            bare,
            "any",
            claimed_by="w",
            now=_NOW,
            event_id="evt-x",
        )


# ===========================================================================
# Injection invariants (no implicit uuid / now)
# ===========================================================================


def test_claim_module_does_not_import_uuid():
    """event_id is injected. The module MUST NOT carry a uuid
    import; that would let a future regression silently call
    uuid.uuid4() at the claim layer and break test
    determinism."""
    seen = set()
    for _, member in vars(claim_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


# ===========================================================================
# Smoke checks
# ===========================================================================


def test_claim_module_has_no_io_imports():
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
    for _, member in vars(claim_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"claim module imports I/O libs: {sorted(leaked)}."
    )


def test_claim_module_exposes_no_reasoning_or_emit_callables():
    """Phase 4's runtime layer must not carry execution-style
    surfaces — reasoning, emit, delegation, sub-agent transfer
    are all later-phase work."""
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(claim_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"claim module exposes execution-suggestive "
                f"callable: {name}"
            )
