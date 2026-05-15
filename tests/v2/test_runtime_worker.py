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
    # event_id_factory was called once (for the would-be claim
    # event) but the claim itself was refused and rolled back —
    # so no events landed.
    assert seed.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


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
