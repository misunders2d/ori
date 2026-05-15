"""Tests for ``app.v2.runtime.recovery``.

Pins per ``docs/PHASE_4_PLAN.md`` §5.3 plus the standard
runtime-module hygiene checks (no implicit uuid / now, no I/O
imports, no execution-suggestive callables).

Behavioural coverage:
- Empty DB → empty list.
- Pending row with past due_at → NOT recovered (only
  claimed/running statuses are scan candidates).
- Claimed within ``claimed_timeout`` → not recovered.
- Claimed past ``claimed_timeout`` → RecoveredRun with the
  full two-row remediation: stale row terminal-failed, NEW
  pending row inserted with parent_run_id / root_run_id
  carried / attempt+1 / fire_reason='retry' / due_at=now;
  events ``run_failed`` + ``run_retry_scheduled`` appear and
  correlate; ``RecoveredRun.new_pending_run_id`` populated.
- Running within ``running_timeout`` → not recovered.
- Running past ``running_timeout`` → same two-row remediation.
- Running with ``started_at IS NULL`` → defensive case;
  treated as immediately stale.
- Cursor independence: a row stale by the running threshold
  but with status='claimed' (or vice versa) must NOT recover.
- Non-silent remediation failure: monkey-patch _remediate's
  inner UPDATE to fail; expect a ``RecoveryError`` in the
  result list, the helper's ``logger.error`` called, and
  subsequent rows still attempted.

Structural / hygiene:
- assert_connection_ready called.
- Naive ``now`` raises NaiveDatetimeError before any SQL runs.
- ``run_id_factory`` and ``event_id_factory`` are required
  kwargs (no defaults); pin via inspect on the signature.
- ``claimed_timeout`` / ``running_timeout`` are required
  kwargs (no defaults).
- assert_legal_transition is called for each remediation with
  (prior_status, FAILED) — pin via monkeypatch.
- Module imports neither uuid nor datetime.now() at the top
  level (no implicit clock / id generation).
- No I/O imports.
- No reasoning/emit/delegate/transfer/sub_agent/dispatch
  callables.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import EventKind, FireReason, RecoveryPolicy, RunStatus
from app.v2.migrations import runner
from app.v2.runtime import recovery as recovery_mod
from app.v2.runtime.recovery import (
    RecoveredRun,
    RecoveryError,
    scan_stale_runs,
)
from app.v2.runtime.state_machine import IllegalTransitionError
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.serialization import NaiveDatetimeError


# Boot reference time used across every test. Fixed so cutoff
# arithmetic is easy to reason about.
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
    attempt: int = 1,
    root_run_id: str | None = None,
    parent_run_id: str | None = None,
    due_at: datetime = _NOW,
    claimed_at: datetime | None = None,
    started_at: datetime | None = None,
    claimed_by: str | None = None,
) -> None:
    if root_run_id is None:
        # Honour the first-attempt self-reference invariant.
        root_run_id = run_id if attempt == 1 else run_id
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id, parent_run_id, claimed_by, claimed_at, "
        " started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            schedule_id,
            "scheduled",
            due_at.isoformat(),
            status,
            attempt,
            root_run_id,
            parent_run_id,
            claimed_by,
            claimed_at.isoformat() if claimed_at else None,
            started_at.isoformat() if started_at else None,
        ),
    )


def _make_factories():
    """Returns deterministic id factories with seen-call counters.

    Each factory returns ``<prefix>-N`` where N increments per
    call so tests can assert exact insertion ids without
    smuggling state through fixtures.
    """
    state = {"run": 0, "evt": 0}

    def run_id_factory() -> str:
        state["run"] += 1
        return f"new-run-{state['run']}"

    def event_id_factory() -> str:
        state["evt"] += 1
        return f"new-evt-{state['evt']}"

    return run_id_factory, event_id_factory, state


def _status(conn: sqlite3.Connection, run_id: str) -> str:
    return conn.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0]


def _row(conn: sqlite3.Connection, run_id: str) -> dict:
    cur = conn.execute(
        "SELECT id, schedule_id, fire_reason, due_at, status, "
        "attempt, root_run_id, parent_run_id, claimed_by, "
        "claimed_at, started_at, completed_at, error "
        "FROM runs WHERE id = ?",
        (run_id,),
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    assert row is not None, f"run {run_id!r} missing"
    return dict(zip(cols, row))


def _events(
    conn: sqlite3.Connection,
    *,
    run_id: str | None = None,
) -> list[dict]:
    if run_id is None:
        cur = conn.execute(
            "SELECT id, run_id, kind, correlates, payload_json "
            "FROM events ORDER BY ts, id"
        )
    else:
        cur = conn.execute(
            "SELECT id, run_id, kind, correlates, payload_json "
            "FROM events WHERE run_id = ? ORDER BY ts, id",
            (run_id,),
        )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ===========================================================================
# Empty / no-op paths
# ===========================================================================


def test_empty_db_returns_empty_list(tmp_path):
    conn = _migrate(tmp_path)
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []


def test_pending_row_not_recovered_even_if_due_past(tmp_path):
    """Only claimed / running statuses are scan candidates. A
    pending row with a past due_at is the worker's job, not
    recovery's."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    old_due = _NOW - timedelta(hours=1)
    _seed_run(
        conn,
        run_id="pending-stale",
        status="pending",
        due_at=old_due,
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []
    assert _status(conn, "pending-stale") == "pending"


# ===========================================================================
# Claimed-stale path
# ===========================================================================


def test_claimed_within_timeout_not_recovered(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="r-fresh",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=1),
        claimed_by="worker-1",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []
    assert _status(conn, "r-fresh") == "claimed"


def test_claimed_past_timeout_two_row_remediation(tmp_path):
    """The load-bearing happy-path test.

    Stale claimed row → terminal failed. New pending row
    inserted with the retry-chain links. Events ``run_failed``
    on the stale row + ``run_retry_scheduled`` on the new row,
    correlated."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="stale-claimed",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=30),
        claimed_by="worker-dead",
        attempt=1,
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert len(result) == 1
    item = result[0]
    assert isinstance(item, RecoveredRun)
    assert item.run_id == "stale-claimed"
    assert item.schedule_id == "daily_audit"
    assert item.prior_status == RunStatus.CLAIMED
    assert item.applied_policy == RecoveryPolicy.QUEUE_RETRY
    assert item.new_pending_run_id == "new-run-1"

    # Stale row terminal-failed.
    stale = _row(conn, "stale-claimed")
    assert stale["status"] == "failed"
    assert stale["completed_at"] == _NOW.isoformat()
    assert "boot recovery" in (stale["error"] or "")

    # New pending row carries retry-chain links.
    new = _row(conn, "new-run-1")
    assert new["status"] == "pending"
    assert new["fire_reason"] == FireReason.RETRY.value
    assert new["attempt"] == 2
    assert new["parent_run_id"] == "stale-claimed"
    assert new["root_run_id"] == "stale-claimed"  # carried from stale's root
    assert new["due_at"] == _NOW.isoformat()

    # Events appear and correlate.
    failed_events = _events(conn, run_id="stale-claimed")
    retry_events = _events(conn, run_id="new-run-1")
    assert len(failed_events) == 1
    assert failed_events[0]["kind"] == EventKind.RUN_FAILED.value
    assert failed_events[0]["id"] == "new-evt-1"
    assert failed_events[0]["correlates"] is None

    assert len(retry_events) == 1
    assert retry_events[0]["kind"] == EventKind.RUN_RETRY_SCHEDULED.value
    assert retry_events[0]["id"] == "new-evt-2"
    assert retry_events[0]["correlates"] == "new-evt-1"


def test_claimed_stale_carries_root_run_id_for_chained_retry(tmp_path):
    """A second-attempt row going stale must carry the original
    chain root, not its own id."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # First attempt — already failed long ago.
    _seed_run(
        conn,
        run_id="root-run",
        status="failed",
        attempt=1,
    )
    # Second attempt — claimed and stale.
    _seed_run(
        conn,
        run_id="stale-attempt-2",
        status="claimed",
        attempt=2,
        root_run_id="root-run",
        parent_run_id="root-run",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    new = _row(conn, "new-run-1")
    assert new["attempt"] == 3
    assert new["root_run_id"] == "root-run"  # carried, not new-run-1
    assert new["parent_run_id"] == "stale-attempt-2"


# ===========================================================================
# Running-stale path
# ===========================================================================


def test_running_within_timeout_not_recovered(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="r-running-fresh",
        status="running",
        claimed_at=_NOW - timedelta(minutes=10),
        started_at=_NOW - timedelta(minutes=5),
        claimed_by="worker-1",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []
    assert _status(conn, "r-running-fresh") == "running"


def test_running_past_timeout_two_row_remediation(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="stale-running",
        status="running",
        claimed_at=_NOW - timedelta(hours=2),
        started_at=_NOW - timedelta(hours=1),
        claimed_by="worker-dead",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert len(result) == 1
    item = result[0]
    assert isinstance(item, RecoveredRun)
    assert item.prior_status == RunStatus.RUNNING
    assert item.applied_policy == RecoveryPolicy.QUEUE_RETRY
    assert item.new_pending_run_id == "new-run-1"
    assert _status(conn, "stale-running") == "failed"
    new = _row(conn, "new-run-1")
    assert new["status"] == "pending"
    assert new["fire_reason"] == FireReason.RETRY.value


def test_running_with_null_started_at_is_immediately_stale(tmp_path):
    """Defensive case: phase-4 invariant says a row in
    ``running`` status MUST have ``started_at`` set, but if the
    invariant ever regresses the scan treats NULL as
    immediately stale rather than silently never-recovering."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="running-no-started",
        status="running",
        claimed_at=_NOW - timedelta(minutes=10),
        started_at=None,
        claimed_by="worker-1",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert len(result) == 1
    assert isinstance(result[0], RecoveredRun)
    assert result[0].prior_status == RunStatus.RUNNING


# ===========================================================================
# Cursor independence (round-2 regression)
# ===========================================================================


def test_claimed_row_uses_claimed_timeout_not_running_timeout(tmp_path):
    """A claimed row whose ``claimed_at`` is older than the
    RUNNING threshold but YOUNGER than the CLAIMED threshold
    must NOT recover. The earlier-draft single-cursor bug would
    false-recover this row."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="r-claimed-borderline",
        status="claimed",
        # 10 minutes ago: PAST a 5-minute running cutoff but
        # YOUNGER than a 30-minute claimed cutoff.
        claimed_at=_NOW - timedelta(minutes=10),
        claimed_by="worker-1",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=30),
        running_timeout=timedelta(minutes=5),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []
    assert _status(conn, "r-claimed-borderline") == "claimed"


def test_running_row_uses_running_timeout_not_claimed_timeout(tmp_path):
    """Mirror image: a running row whose ``started_at`` is
    older than the CLAIMED threshold but younger than the
    RUNNING threshold must NOT recover."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="r-running-borderline",
        status="running",
        claimed_at=_NOW - timedelta(minutes=20),
        # Started 10 minutes ago: past a 5-minute claimed
        # cutoff but younger than a 30-minute running cutoff.
        started_at=_NOW - timedelta(minutes=10),
        claimed_by="worker-1",
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    assert result == []
    assert _status(conn, "r-running-borderline") == "running"


# ===========================================================================
# Determinism / ordering
# ===========================================================================


def test_result_order_is_deterministic(tmp_path):
    """Result list orders claimed-stale rows first (by
    ``claimed_at, id``) and running-stale rows after (by
    ``started_at, id``). Pins so a future SELECT-without-ORDER
    regression flags here."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_schedule(conn, "other_sched")
    # Two claimed-stale rows with reversed insertion order.
    _seed_run(
        conn,
        run_id="claim-b",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=20),
    )
    _seed_run(
        conn,
        run_id="claim-a",
        status="claimed",
        schedule_id="other_sched",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    # Two running-stale rows.
    _seed_run(
        conn,
        run_id="run-y",
        status="running",
        claimed_at=_NOW - timedelta(hours=2),
        started_at=_NOW - timedelta(hours=1),
    )
    _seed_run(
        conn,
        run_id="run-x",
        status="running",
        schedule_id="other_sched",
        claimed_at=_NOW - timedelta(hours=3),
        started_at=_NOW - timedelta(hours=2),
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    # Claimed-stale first (sorted by claimed_at ASC):
    #   claim-a (30 min ago) → claim-b (20 min ago)
    # Then running-stale (sorted by started_at ASC):
    #   run-x (2h ago) → run-y (1h ago)
    order = [r.run_id for r in result]
    assert order == ["claim-a", "claim-b", "run-x", "run-y"]


# ===========================================================================
# Non-silent failure
# ===========================================================================


def test_remediation_failure_yields_recovery_error_and_logs(
    tmp_path, caplog
):
    """Force the new pending Run INSERT to fail by having the
    first call to ``run_id_factory`` return an id that already
    exists in the ``runs`` table — the INSERT raises
    ``IntegrityError`` on the PK collision. Expect:

    - Result list contains a ``RecoveryError`` for the affected
      row (not a silent drop).
    - ``logger.error`` was called.
    - The transaction's ROLLBACK reverted the stale row's
      UPDATE; it stays in its original status.
    - Subsequent rows are still attempted (the helper continues
      past a single-row failure).
    """
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # Pre-seed a row whose id the first remediation will try to
    # use. The PK collision makes the INSERT INTO runs fail.
    _seed_run(
        conn,
        run_id="collision-id",
        status="failed",  # terminal — neither stale candidate.
        attempt=1,
    )
    # Two claimed-stale rows: the first will collide on the new
    # pending Run insert, the second must still be attempted.
    _seed_run(
        conn,
        run_id="claim-fail",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    _seed_run(
        conn,
        run_id="claim-ok",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=20),
    )

    # First call returns the collision id; subsequent calls
    # return unique ones so the second remediation succeeds.
    call_state = {"i": 0}

    def run_id_factory():
        call_state["i"] += 1
        if call_state["i"] == 1:
            return "collision-id"
        return f"new-run-{call_state['i']}"

    event_state = {"i": 0}

    def event_id_factory():
        event_state["i"] += 1
        return f"new-evt-{event_state['i']}"

    with caplog.at_level(logging.ERROR, logger=recovery_mod.__name__):
        result = scan_stale_runs(
            conn,
            now=_NOW,
            claimed_timeout=timedelta(minutes=5),
            running_timeout=timedelta(minutes=30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
    assert len(result) == 2
    failed_item, ok_item = result
    assert isinstance(failed_item, RecoveryError)
    assert failed_item.run_id == "claim-fail"
    assert failed_item.prior_status == RunStatus.CLAIMED
    # IntegrityError surfaces UNIQUE / PK collision wording.
    assert "UNIQUE" in failed_item.error_message or "PRIMARY KEY" in failed_item.error_message

    assert isinstance(ok_item, RecoveredRun)
    assert ok_item.run_id == "claim-ok"

    # Logger error fired for the failure.
    assert any(
        r.levelno == logging.ERROR and "claim-fail" in r.getMessage()
        for r in caplog.records
    )

    # Rollback was effective: the failed row stayed claimed.
    assert _status(conn, "claim-fail") == "claimed"
    # The ok row was actually remediated.
    assert _status(conn, "claim-ok") == "failed"
    # The collision row is untouched (not modified by the scan).
    assert _status(conn, "collision-id") == "failed"


# ===========================================================================
# Timeout validation (reviewer round-4 blocker — must be > 0)
# ===========================================================================


def test_claimed_timeout_zero_rejected(tmp_path):
    """Zero cutoff would mark every claimed row stale (cutoff
    == now). Reject before any SQL or factory call."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="claim-1",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    run_id_factory, event_id_factory, state = _make_factories()
    with pytest.raises(ValueError, match="claimed_timeout"):
        scan_stale_runs(
            conn,
            now=_NOW,
            claimed_timeout=timedelta(0),
            running_timeout=timedelta(minutes=30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
    # Side-effect free: row untouched, factories never called.
    assert _status(conn, "claim-1") == "claimed"
    assert state == {"run": 0, "evt": 0}


def test_claimed_timeout_negative_rejected(tmp_path):
    """Negative cutoff would flip into the future, mass-failing
    fresh claimed rows."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="claim-1",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=1),
    )
    run_id_factory, event_id_factory, state = _make_factories()
    with pytest.raises(ValueError, match="claimed_timeout"):
        scan_stale_runs(
            conn,
            now=_NOW,
            claimed_timeout=timedelta(minutes=-5),
            running_timeout=timedelta(minutes=30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
    assert _status(conn, "claim-1") == "claimed"
    assert state == {"run": 0, "evt": 0}


def test_running_timeout_zero_rejected(tmp_path):
    conn = _migrate(tmp_path)
    run_id_factory, event_id_factory, state = _make_factories()
    with pytest.raises(ValueError, match="running_timeout"):
        scan_stale_runs(
            conn,
            now=_NOW,
            claimed_timeout=timedelta(minutes=5),
            running_timeout=timedelta(0),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
    assert state == {"run": 0, "evt": 0}


def test_running_timeout_negative_rejected(tmp_path):
    conn = _migrate(tmp_path)
    run_id_factory, event_id_factory, state = _make_factories()
    with pytest.raises(ValueError, match="running_timeout"):
        scan_stale_runs(
            conn,
            now=_NOW,
            claimed_timeout=timedelta(minutes=5),
            running_timeout=timedelta(minutes=-30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
    assert state == {"run": 0, "evt": 0}


# ===========================================================================
# Inactive-schedule retry insertion (deferred pause/archive policy)
# ===========================================================================


def test_paused_schedule_stale_row_still_remediated_retry_unclaimable(
    tmp_path,
):
    """Recovery inserts a retry row unconditionally — it does
    NOT consult ``schedules.status``. For a paused or archived
    schedule the retry row stays pending forever because
    ``claim_run``'s schedule-status predicate refuses it.
    Pause/archive cancellation of pending runs (including
    recovery-inserted ones) is the ``paused_pending_policy``
    open question in PHASE_4_PLAN §12 item 5, deferred to a
    later phase.

    This test pins phase-4's current behaviour so a future
    pause/archive phase that flips it has an explicit failure
    point to update."""
    from app.v2.runtime.claim import claim_run

    conn = _migrate(tmp_path)
    _seed_schedule(conn, status="paused")
    _seed_run(
        conn,
        run_id="paused-stale",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    # Recovery still queued a retry row.
    assert len(result) == 1
    assert isinstance(result[0], RecoveredRun)
    new = _row(conn, "new-run-1")
    assert new["status"] == "pending"

    # claim_run refuses the retry because the schedule is
    # paused — no execution leak.
    later = _NOW + timedelta(minutes=1)
    claimed = claim_run(
        conn,
        "new-run-1",
        claimed_by="worker-1",
        now=later,
        event_id="probe-evt",
    )
    assert claimed is False
    assert _status(conn, "new-run-1") == "pending"


# ===========================================================================
# Datetime / connection guards
# ===========================================================================


def test_naive_now_raises_naive_datetime_error(tmp_path):
    conn = _migrate(tmp_path)
    run_id_factory, event_id_factory, _ = _make_factories()
    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(NaiveDatetimeError, match="now"):
        scan_stale_runs(
            conn,
            now=naive,
            claimed_timeout=timedelta(minutes=5),
            running_timeout=timedelta(minutes=30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )


def test_scan_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    run_id_factory, event_id_factory, _ = _make_factories()
    with pytest.raises(ConnectionNotReady):
        scan_stale_runs(
            bare,
            now=_NOW,
            claimed_timeout=timedelta(minutes=5),
            running_timeout=timedelta(minutes=30),
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )


# ===========================================================================
# State-machine policy gate
# ===========================================================================


def test_remediation_consults_state_machine(
    tmp_path, monkeypatch
):
    """Pin that ``assert_legal_transition`` is called for each
    remediation with ``(prior_status, FAILED)``. Same defence
    pattern as claim_run: future narrowing of
    ``LEGAL_TRANSITIONS`` surfaces here as a RecoveryError
    rather than silently writing an illegal transition."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(
        conn,
        run_id="stale-c",
        status="claimed",
        claimed_at=_NOW - timedelta(minutes=30),
    )
    _seed_run(
        conn,
        run_id="stale-r",
        status="running",
        claimed_at=_NOW - timedelta(hours=2),
        started_at=_NOW - timedelta(hours=1),
    )

    calls: list[tuple[RunStatus, RunStatus]] = []

    def _fake_assert(src, dst):
        calls.append((src, dst))
        raise IllegalTransitionError(
            f"forbidden: {src.value}→{dst.value}"
        )

    monkeypatch.setattr(
        recovery_mod, "assert_legal_transition", _fake_assert
    )

    run_id_factory, event_id_factory, _ = _make_factories()
    result = scan_stale_runs(
        conn,
        now=_NOW,
        claimed_timeout=timedelta(minutes=5),
        running_timeout=timedelta(minutes=30),
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
    # Both attempted, both surfaced as RecoveryError (caught
    # IllegalTransitionError boundary).
    assert len(result) == 2
    assert all(isinstance(item, RecoveryError) for item in result)
    assert calls == [
        (RunStatus.CLAIMED, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.FAILED),
    ]
    # Neither row was mutated.
    assert _status(conn, "stale-c") == "claimed"
    assert _status(conn, "stale-r") == "running"


# ===========================================================================
# Signature / kwargs pins
# ===========================================================================


def test_scan_signature_requires_all_kwargs():
    """``claimed_timeout`` / ``running_timeout`` /
    ``run_id_factory`` / ``event_id_factory`` MUST be required
    keyword-only — no defaults. Forces caller to think about
    them per plan §5.4."""
    sig = inspect.signature(scan_stale_runs)
    params = sig.parameters
    required_kwonly = [
        "now",
        "claimed_timeout",
        "running_timeout",
        "run_id_factory",
        "event_id_factory",
    ]
    for name in required_kwonly:
        assert name in params, f"missing kwarg {name!r}"
        param = params[name]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only"
        )
        assert param.default is inspect.Parameter.empty, (
            f"{name!r} must have no default"
        )


# ===========================================================================
# Hygiene smoke tests (mirror claim.py smoke set)
# ===========================================================================


def test_recovery_module_does_not_import_uuid():
    """ID generation is injected. The module MUST NOT carry a
    uuid import that a future regression could call to
    silently generate ids at the recovery layer."""
    seen = set()
    for _, member in vars(recovery_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_recovery_module_has_no_io_imports():
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
    for _, member in vars(recovery_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"recovery module imports I/O libs: {sorted(leaked)}."
    )


def test_recovery_module_exposes_no_reasoning_or_emit_callables():
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(recovery_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"recovery module exposes execution-suggestive "
                f"callable: {name}"
            )
