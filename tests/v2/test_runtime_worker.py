"""Tests for ``app.v2.runtime.worker``.

Pins per ``docs/PHASE_4_PLAN.md`` §6.4.

Behavioural coverage:
- Single tick walk: pending → claimed → running → succeeded.
- Per-tick claims at most ONE row (``limit=1``).
- All three worker-emitted events land: run_claimed,
  run_started, run_succeeded. Each correlates / attributes
  correctly. ``started_at`` and ``completed_at`` populate.
- No-pending tick returns None without side effects.
- Paused schedule + pending row → tick returns None; the
  predicate inside ``claim_run`` is what catches it.
- Two workers on distinct schedules each claim their own run
  (single-flight is per-schedule).
- Cooperative shutdown: ``start()`` then ``stop()`` cleanly
  drains the loop; in-flight tick finishes before close.
- ``stop()`` before ``start()`` is a no-op.
- ``start()`` twice raises RuntimeError.

State-machine + injection pins:
- ``assert_legal_transition`` called twice per successful
  tick: (CLAIMED, RUNNING) then (RUNNING, SUCCEEDED).
  Monkey-patch raise on the second call yields a partial
  walk: row stays running, the run_started event is present,
  run_succeeded never appears.
- ``run_id_factory`` is NEVER called during a successful tick
  (phase 4 worker body has no insertion path that uses it;
  pin so a regression that quietly starts using it surfaces).
- ``event_id_factory`` called exactly 3 times per successful
  tick (claim + started + succeeded).
- ``clock`` called multiple times per tick — every timestamp
  the worker stores comes from there, never datetime.now().

Constructor validation:
- Empty ``worker_id`` rejected.
- Zero / negative ``poll_interval`` rejected.
- Non-callable factories / clock rejected.

Hygiene smoke:
- No ``uuid`` import.
- No I/O-library imports.
- No reasoning / emit / delegate / transfer / sub_agent /
  dispatch / invoke public callable.
- Module source does not call ``datetime.now()`` or
  ``uuid.uuid4()``.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from app.v2.enums import EventKind, RunStatus, ScheduleStatus
from app.v2.migrations import runner
from app.v2.runtime import worker as worker_mod
from app.v2.runtime.state_machine import IllegalTransitionError
from app.v2.runtime.worker import Worker


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _migrate(tmp_path: Path, name: str = "scheduler.db") -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / name))
    runner.apply_pending(conn)
    return conn


def _conn_factory_for(tmp_path: Path, name: str = "scheduler.db"):
    """Return a callable that opens FRESH connections to the
    same DB file each call. Workers close their own connection
    on stop() / per-tick teardown; tests inspect via a separate
    long-lived connection — the factory's freshly-opened
    instances are independent of the test's own conn.
    """
    db = tmp_path / name

    def _factory():
        conn = sqlite3.connect(str(db))
        runner.apply_pending(conn)
        return conn

    return _factory


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
    due_at: datetime = _NOW,
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
            due_at.isoformat(),
            status,
            1,
            run_id,
        ),
    )


def _status(conn: sqlite3.Connection, run_id: str) -> str:
    return conn.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0]


def _row(conn: sqlite3.Connection, run_id: str) -> dict:
    cur = conn.execute(
        "SELECT id, schedule_id, status, claimed_by, claimed_at, "
        "started_at, completed_at, error FROM runs WHERE id = ?",
        (run_id,),
    )
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(cols, row))


def _events(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    cur = conn.execute(
        "SELECT id, kind, payload_json, correlates, ts "
        "FROM events WHERE run_id = ? ORDER BY ts, id",
        (run_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _counter():
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return f"id-{state['i']}"

    return factory, state


def _evt_counter():
    return _evt_counter_prefix("evt")


def _evt_counter_prefix(prefix: str):
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return f"{prefix}-{state['i']}"

    return factory, state


def _run_counter():
    state = {"i": 0, "calls": []}

    def factory():
        state["i"] += 1
        # Pin call-site too so the tick can't quietly call this.
        state["calls"].append(state["i"])
        return f"new-run-{state['i']}"

    return factory, state


def _fixed_clock(value: datetime = _NOW):
    state = {"calls": 0}

    def clock():
        state["calls"] += 1
        return value

    return clock, state


def _make_worker(
    factory: Callable[[], sqlite3.Connection],
    worker_id: str = "worker-1",
    *,
    poll_interval: timedelta = timedelta(seconds=10),
    clock_value: datetime = _NOW,
    evt_prefix: str = "evt",
):
    evt_factory, evt_state = _evt_counter_prefix(evt_prefix)
    run_factory, run_state = _run_counter()
    clock, clock_state = _fixed_clock(clock_value)
    worker = Worker(
        conn_factory=factory,
        worker_id=worker_id,
        poll_interval=poll_interval,
        clock=clock,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    return worker, {
        "evt": evt_state,
        "run": run_state,
        "clock": clock_state,
    }


# ===========================================================================
# Single-tick happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_single_tick_walks_lifecycle(tmp_path):
    """The load-bearing happy-path test.

    pending → claimed → running → succeeded with the three
    worker-emitted events landing in the same TX as their
    transitions. claim_run writes its own run_claimed; the
    worker writes run_started + run_succeeded."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()

    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result == "r-1"

    row = _row(seed, "r-1")
    assert row["status"] == RunStatus.SUCCEEDED.value
    assert row["claimed_by"] == "worker-1"
    assert row["claimed_at"] == _NOW.isoformat()
    assert row["started_at"] == _NOW.isoformat()
    assert row["completed_at"] == _NOW.isoformat()

    events = _events(seed, "r-1")
    assert [e["kind"] for e in events] == [
        EventKind.RUN_CLAIMED.value,
        EventKind.RUN_STARTED.value,
        EventKind.RUN_SUCCEEDED.value,
    ]
    # run_started correlates to the claim event.
    assert events[1]["correlates"] == events[0]["id"]
    # Worker id is recorded on the started + succeeded events.
    assert '"worker_id":"worker-1"' in events[1]["payload_json"]
    assert '"worker_id":"worker-1"' in events[2]["payload_json"]


@pytest.mark.asyncio
async def test_single_tick_uses_event_id_factory_three_times(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    await worker.tick()
    # claim + run_started + run_succeeded
    assert counters["evt"]["i"] == 3


@pytest.mark.asyncio
async def test_single_tick_does_not_call_run_id_factory(tmp_path):
    """run_id_factory is plumbed for later phases (retry path).
    Phase 4's empty body must not insert new Run rows. Pin via
    a counter that records every call."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    await worker.tick()
    assert counters["run"]["i"] == 0
    assert counters["run"]["calls"] == []


@pytest.mark.asyncio
async def test_single_tick_uses_clock(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    await worker.tick()
    # poll cursor + started_at + completed_at = 3 calls. The
    # tick must never reach datetime.now() — the clock is the
    # sole source for every stored timestamp.
    assert counters["clock"]["calls"] >= 3


# ===========================================================================
# No-op paths
# ===========================================================================


@pytest.mark.asyncio
async def test_tick_with_no_pending_returns_none(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result is None
    # Nothing inserted into events; nothing transitioned.
    assert seed.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    # event_id_factory NOT called when no pending row found.
    assert counters["evt"]["i"] == 0


@pytest.mark.asyncio
async def test_tick_skips_pending_with_future_due_at(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    future = _NOW + timedelta(hours=1)
    _seed_run(seed, run_id="future", due_at=future)
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result is None
    assert _status(seed, "future") == "pending"


@pytest.mark.asyncio
async def test_tick_on_paused_schedule_does_not_claim(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed, status="paused")
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result is None
    assert _status(seed, "r-1") == "pending"
    # list_claimable_due's schedule-status predicate drops the
    # paused row at the SQL layer — the worker never even tries
    # to claim, so event_id_factory was never called. (Before
    # the round-5 pre-filter the worker would have generated +
    # discarded one id per attempted-then-refused claim.)
    assert counters["evt"]["i"] == 0
    assert seed.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


# ===========================================================================
# Starvation regressions (reviewer round-4 blocker 1)
# ===========================================================================


@pytest.mark.asyncio
async def test_paused_oldest_does_not_starve_active_newer(tmp_path):
    """Reviewer round-4 starvation regression.

    Schedule A is paused; its pending row has the oldest
    due_at. Schedule B is active; its pending row is newer.
    The claimable-due read filters the paused row out of the
    batch entirely, so the tick processes B's run on its
    first attempt."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed, schedule_id="paused_sched", status="paused")
    _seed_schedule(seed, schedule_id="active_sched", status="active")
    older = _NOW - timedelta(minutes=10)
    newer = _NOW - timedelta(minutes=5)
    _seed_run(
        seed,
        run_id="paused-old",
        schedule_id="paused_sched",
        due_at=older,
    )
    _seed_run(
        seed,
        run_id="active-new",
        schedule_id="active_sched",
        due_at=newer,
    )
    seed.commit()
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result == "active-new"
    assert _status(seed, "active-new") == "succeeded"
    # Paused row stays pending — recovery / pause-policy phase
    # owns explicit cancellation.
    assert _status(seed, "paused-old") == "pending"
    # No wasted event ids: the pre-filter dropped paused-old
    # so claim_run was only invoked for active-new (1 claim +
    # 1 run_started + 1 run_succeeded = 3 ids).
    assert counters["evt"]["i"] == 3


@pytest.mark.asyncio
async def test_eleven_blocked_older_does_not_starve_active(tmp_path):
    """Reviewer round-5 starvation regression.

    With the default ``claim_batch_size=10`` and ELEVEN
    paused-schedule pending rows older than the one active
    row, a naive bounded-batch reader would only see the 11
    blocked rows every tick and the active row would starve
    forever. The claimable-due pre-filter rejects all 11
    blocked rows at the SQL layer, so the batch contains only
    the active row and the tick processes it on the first
    attempt."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed, schedule_id="paused_sched", status="paused")
    _seed_schedule(seed, schedule_id="active_sched", status="active")
    # 11 older pending rows on the paused schedule.
    for i in range(11):
        due = _NOW - timedelta(minutes=60 - i)
        _seed_run(
            seed,
            run_id=f"paused-{i:02d}",
            schedule_id="paused_sched",
            due_at=due,
        )
    # One newer pending row on the active schedule.
    _seed_run(
        seed,
        run_id="active-new",
        schedule_id="active_sched",
        due_at=_NOW - timedelta(minutes=1),
    )
    seed.commit()
    # Use the default claim_batch_size=10 — that's the value
    # the round-5 reviewer pointed at.
    worker, counters = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result == "active-new"
    assert _status(seed, "active-new") == "succeeded"
    # All 11 paused rows untouched.
    for i in range(11):
        assert _status(seed, f"paused-{i:02d}") == "pending"
    # No wasted event ids — pre-filter dropped all blocked
    # rows; only the active row's lifecycle consumed ids.
    assert counters["evt"]["i"] == 3


@pytest.mark.asyncio
async def test_same_schedule_blocked_does_not_starve_different_schedule(
    tmp_path,
):
    """Reviewer round-4 blocker 1 regression, single-flight
    variant.

    Schedule A already has a running row + an OLDER pending
    row (blocked by the per-schedule single-flight predicate
    inside claim_run). Schedule B has a NEWER pending row.
    The batched tick must skip A's blocked pending and claim
    B's pending."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed, schedule_id="sched_a")
    _seed_schedule(seed, schedule_id="sched_b")
    # A has a running row (single-flight will block its pending).
    seed.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "a-running",
            "sched_a",
            "scheduled",
            (_NOW - timedelta(hours=1)).isoformat(),
            "running",
            1,
            "a-running",
            (_NOW - timedelta(minutes=30)).isoformat(),
        ),
    )
    older = _NOW - timedelta(minutes=10)
    newer = _NOW - timedelta(minutes=5)
    _seed_run(
        seed,
        run_id="a-pending-old",
        schedule_id="sched_a",
        due_at=older,
    )
    _seed_run(
        seed,
        run_id="b-pending-new",
        schedule_id="sched_b",
        due_at=newer,
    )
    seed.commit()
    worker, _ = _make_worker(_conn_factory_for(tmp_path))
    result = await worker.tick()
    assert result == "b-pending-new"
    assert _status(seed, "b-pending-new") == "succeeded"
    # A's blocked pending stays pending; A's running row stays
    # running.
    assert _status(seed, "a-pending-old") == "pending"
    assert _status(seed, "a-running") == "running"


@pytest.mark.asyncio
async def test_tick_claims_at_most_one_row_even_when_batch_holds_many(
    tmp_path,
):
    """Two active schedules, each with a pending row. The
    batch read returns both, but the tick claims ONE and
    returns — the second stays pending for the next tick."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed, schedule_id="sched_a")
    _seed_schedule(seed, schedule_id="sched_b")
    _seed_run(
        seed,
        run_id="a-1",
        schedule_id="sched_a",
        due_at=_NOW - timedelta(minutes=10),
    )
    _seed_run(
        seed,
        run_id="b-1",
        schedule_id="sched_b",
        due_at=_NOW - timedelta(minutes=5),
    )
    seed.commit()
    worker, _ = _make_worker(_conn_factory_for(tmp_path))
    first = await worker.tick()
    # Oldest claimable wins.
    assert first == "a-1"
    assert _status(seed, "a-1") == "succeeded"
    assert _status(seed, "b-1") == "pending"


def test_claim_batch_size_must_be_positive(tmp_path):
    evt, _ = _evt_counter()
    run, _ = _run_counter()
    clock, _ = _fixed_clock()
    for bad in (0, -1, -10):
        with pytest.raises(ValueError, match="claim_batch_size"):
            Worker(
                conn_factory=_conn_factory_for(tmp_path),
                worker_id="w",
                poll_interval=timedelta(seconds=1),
                clock=clock,
                run_id_factory=run,
                event_id_factory=evt,
                claim_batch_size=bad,
            )


# ===========================================================================
# State-machine policy gate
# ===========================================================================


@pytest.mark.asyncio
async def test_state_machine_called_for_both_transitions(
    tmp_path, monkeypatch
):
    """Pin that ``assert_legal_transition`` is called twice per
    successful tick: (CLAIMED, RUNNING) then (RUNNING,
    SUCCEEDED). Same defence pattern as claim_run and
    scan_stale_runs."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, _ = _make_worker(_conn_factory_for(tmp_path))

    calls: list[tuple[RunStatus, RunStatus]] = []
    real_assert = worker_mod.assert_legal_transition

    def _spy_assert(src, dst):
        calls.append((src, dst))
        return real_assert(src, dst)

    monkeypatch.setattr(worker_mod, "assert_legal_transition", _spy_assert)
    await worker.tick()
    assert calls == [
        (RunStatus.CLAIMED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
    ]


@pytest.mark.asyncio
async def test_state_machine_raise_on_second_transition_halts_walk(
    tmp_path, monkeypatch
):
    """Force ``assert_legal_transition(RUNNING, SUCCEEDED)`` to
    raise. The first transition (claimed→running) lands and so
    does its event; the second never runs, so the row stays
    running and run_succeeded is absent."""
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, _ = _make_worker(_conn_factory_for(tmp_path))

    real_assert = worker_mod.assert_legal_transition
    state = {"i": 0}

    def _fake_assert(src, dst):
        state["i"] += 1
        if state["i"] == 2:
            raise IllegalTransitionError(
                f"synthetic forbid: {src.value}→{dst.value}"
            )
        return real_assert(src, dst)

    monkeypatch.setattr(worker_mod, "assert_legal_transition", _fake_assert)
    with pytest.raises(IllegalTransitionError):
        await worker.tick()
    row = _row(seed, "r-1")
    assert row["status"] == RunStatus.RUNNING.value
    assert row["started_at"] == _NOW.isoformat()
    assert row["completed_at"] is None
    kinds = [e["kind"] for e in _events(seed, "r-1")]
    assert EventKind.RUN_STARTED.value in kinds
    assert EventKind.RUN_SUCCEEDED.value not in kinds


# ===========================================================================
# Concurrent workers
# ===========================================================================


@pytest.mark.asyncio
async def test_two_workers_on_distinct_schedules_each_claim(tmp_path):
    """Each worker owns its own connection; both transition
    their own schedule's pending run. Single-flight is per-
    schedule, so cross-schedule claims do not interfere."""
    db_path = tmp_path / "shared.db"
    # Pre-create + seed via a primary connection so both
    # workers see the same data file.
    primary = sqlite3.connect(str(db_path))
    runner.apply_pending(primary)
    _seed_schedule(primary, schedule_id="sched_a")
    _seed_schedule(primary, schedule_id="sched_b")
    _seed_run(primary, run_id="r-a", schedule_id="sched_a")
    _seed_run(primary, run_id="r-b", schedule_id="sched_b")
    primary.commit()
    primary.close()

    def _factory_for(schedule_id: str):
        def _open():
            conn = sqlite3.connect(str(db_path))
            runner.apply_pending(conn)
            return conn
        return _open

    # DISJOINT event-id prefixes — both workers write to the
    # same `events` table; same id between two factories would
    # collide on the PK.
    evt_a, _ = _evt_counter_prefix("evt-a")
    evt_b, _ = _evt_counter_prefix("evt-b")
    run_factory_a, _ = _run_counter()
    run_factory_b, _ = _run_counter()
    clock_a, _ = _fixed_clock(_NOW)
    clock_b, _ = _fixed_clock(_NOW)

    worker_a = Worker(
        conn_factory=_factory_for("sched_a"),
        worker_id="w-a",
        poll_interval=timedelta(seconds=10),
        clock=clock_a,
        run_id_factory=run_factory_a,
        event_id_factory=evt_a,
    )
    worker_b = Worker(
        conn_factory=_factory_for("sched_b"),
        worker_id="w-b",
        poll_interval=timedelta(seconds=10),
        clock=clock_b,
        run_id_factory=run_factory_b,
        event_id_factory=evt_b,
    )
    # Run ticks concurrently. Both should succeed because the
    # single-flight predicate is per-schedule.
    result_a, result_b = await asyncio.gather(
        worker_a.tick(), worker_b.tick()
    )
    assert result_a == "r-a"
    assert result_b == "r-b"

    verify = sqlite3.connect(str(db_path))
    runner.apply_pending(verify)
    assert _status(verify, "r-a") == "succeeded"
    assert _status(verify, "r-b") == "succeeded"
    verify.close()


# ===========================================================================
# Lifecycle: start / stop / loop
# ===========================================================================


@pytest.mark.asyncio
async def test_start_then_stop_drains_loop(tmp_path):
    seed = _migrate(tmp_path)
    _seed_schedule(seed)
    _seed_run(seed, run_id="r-1")
    seed.commit()
    worker, _ = _make_worker(
        _conn_factory_for(tmp_path),
        poll_interval=timedelta(milliseconds=10),
    )
    await worker.start()
    # Poll until the worker walks the row through to succeeded.
    succeeded = False
    for _ in range(200):
        await asyncio.sleep(0.01)
        if _status(seed, "r-1") == "succeeded":
            succeeded = True
            break
    await worker.stop()
    assert succeeded
    assert _status(seed, "r-1") == "succeeded"


@pytest.mark.asyncio
async def test_stop_before_start_is_noop(tmp_path):
    _ = _migrate(tmp_path)
    worker, _ = _make_worker(_conn_factory_for(tmp_path))
    # Should not raise / hang / open conn.
    await worker.stop()


@pytest.mark.asyncio
async def test_start_twice_raises(tmp_path):
    _ = _migrate(tmp_path)
    worker, _ = _make_worker(_conn_factory_for(tmp_path))
    await worker.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await worker.start()
    finally:
        await worker.stop()


# ===========================================================================
# Run-loop signal hygiene (reviewer round-4 blocker 2)
# ===========================================================================


@pytest.mark.asyncio
async def test_run_loop_does_not_swallow_cancelled_error(tmp_path):
    """Reviewer round-4 blocker 2 regression.

    asyncio.CancelledError inherits from BaseException
    (Python 3.8+) and is the canonical cancel/shutdown
    signal. The loop's exception handler must catch only
    Exception, so CancelledError propagates instead of being
    logged + retried in a tight loop."""
    seed = _migrate(tmp_path)
    seed.commit()
    worker, _ = _make_worker(
        _conn_factory_for(tmp_path),
        poll_interval=timedelta(milliseconds=10),
    )

    async def _fake_tick():
        raise asyncio.CancelledError("synthetic")

    # Override the bound method on this instance only — the
    # loop's ``await self.tick()`` will resolve to _fake_tick.
    worker.tick = _fake_tick  # type: ignore[method-assign]

    await worker.start()
    task = worker._task
    assert task is not None

    # Poll for task completion. If the loop swallowed
    # CancelledError, the task would never finish (stop_event
    # never set, fake_tick keeps raising forever).
    done = False
    for _ in range(100):
        await asyncio.sleep(0.01)
        if task.done():
            done = True
            break
    assert done, (
        "run loop did not exit on CancelledError — it is "
        "probably catching BaseException and retrying."
    )
    # Task ended with CancelledError. We can't re-await it
    # without re-raising, so manually unwire the worker's
    # internal handles in the same shape stop() would after a
    # clean exit. This is per-test cleanup, not part of the
    # Worker API.
    worker._task = None
    worker._stop_event = None
    if worker._conn is not None:
        worker._conn.close()
        worker._conn = None


def test_run_loop_source_does_not_catch_base_exception():
    """Structural pin to back the behavioural test above. The
    loop's handler line must NOT be ``except BaseException:``
    — anything broader than ``except Exception:`` swallows
    cancel / shutdown signals."""
    source = inspect.getsource(worker_mod)
    assert "except BaseException" not in source, (
        "worker._run_loop must catch Exception, not "
        "BaseException — broader handlers swallow "
        "asyncio.CancelledError and KeyboardInterrupt."
    )


# ===========================================================================
# Constructor validation
# ===========================================================================


def test_worker_id_must_be_non_empty(tmp_path):
    evt, _ = _evt_counter()
    run, _ = _run_counter()
    clock, _ = _fixed_clock()
    with pytest.raises(ValueError, match="worker_id"):
        Worker(
            conn_factory=_conn_factory_for(tmp_path),
            worker_id="",
            poll_interval=timedelta(seconds=1),
            clock=clock,
            run_id_factory=run,
            event_id_factory=evt,
        )


@pytest.mark.parametrize(
    "interval",
    [timedelta(0), timedelta(seconds=-1), timedelta(milliseconds=-1)],
)
def test_poll_interval_must_be_positive(tmp_path, interval):
    evt, _ = _evt_counter()
    run, _ = _run_counter()
    clock, _ = _fixed_clock()
    with pytest.raises(ValueError, match="poll_interval"):
        Worker(
            conn_factory=_conn_factory_for(tmp_path),
            worker_id="w",
            poll_interval=interval,
            clock=clock,
            run_id_factory=run,
            event_id_factory=evt,
        )


@pytest.mark.parametrize("field", ["conn_factory", "clock", "run_id_factory", "event_id_factory"])
def test_non_callable_factory_rejected(tmp_path, field):
    evt, _ = _evt_counter()
    run, _ = _run_counter()
    clock, _ = _fixed_clock()
    kwargs = dict(
        conn_factory=_conn_factory_for(tmp_path),
        worker_id="w",
        poll_interval=timedelta(seconds=1),
        clock=clock,
        run_id_factory=run,
        event_id_factory=evt,
    )
    kwargs[field] = "not-callable"
    with pytest.raises(TypeError, match=field):
        Worker(**kwargs)


# ===========================================================================
# Hygiene smoke
# ===========================================================================


def test_worker_module_does_not_import_uuid():
    seen = set()
    for _, member in vars(worker_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_worker_module_has_no_io_imports():
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
    for _, member in vars(worker_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"worker module imports I/O libs: {sorted(leaked)}."
    )


def test_worker_module_source_has_no_datetime_now_or_uuid_calls():
    """Smoke pin: every timestamp comes from ``clock()``; every
    new id comes from a factory. A regression that quietly
    inserts ``datetime.now()`` or ``uuid.uuid4()`` in the
    worker code path would defeat test determinism."""
    source = inspect.getsource(worker_mod)
    # Strip comments / strings would be nice but inspect.getsource
    # returns the raw source. Restrict to forbidden call patterns
    # that would actually run (not the names mentioned in
    # docstrings).
    assert "datetime.now(" not in source, (
        "worker module must use the injected clock(), not "
        "datetime.now()."
    )
    assert "uuid.uuid4(" not in source, (
        "worker module must use the injected id factories, "
        "not uuid.uuid4()."
    )


def test_worker_module_exposes_no_reasoning_or_emit_callables():
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(worker_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"worker module exposes execution-suggestive "
                f"callable: {name}"
            )
