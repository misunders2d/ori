"""Fast end-to-end integration test for the v2 runtime.

Plan section 5.5.a -- DEFAULT-suite (not @pytest.mark.slow).

Exercises the full wiring chain WITHOUT real wall-clock
wait + WITHOUT going through ``boot_runtime`` or
``SchedulerBinding.start()``:

  binding._fire_for(schedule_id)
    -> wakeup()                 # inserts pending Run + run_created
    -> Run row visible
  worker.tick()
    -> list_claimable_due       # finds the pending Run
    -> claim_run                # promotes pending -> claimed + run_claimed
    -> state_machine gate (claimed -> running)
    -> update_run_status_and_append_event (claimed -> running + run_started)
    -> empty body (await asyncio.sleep(0))
    -> state_machine gate (running -> succeeded)
    -> update_run_status_and_append_event (running -> succeeded + run_succeeded)

Final assertions: run row at status='succeeded' with
started_at + completed_at populated; events table contains
exactly the four expected kinds in insertion order
(run_created -> run_claimed -> run_started -> run_succeeded).

The slow real-AsyncIOScheduler test lives in
test_runtime_e2e_slow.py with @pytest.mark.slow.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    RunStatus,
    ScheduleStatus,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.runtime.binding import SchedulerBinding
from app.v2.runtime.wakeup import wakeup
from app.v2.runtime.worker import Worker
from app.v2.storage.schedules import insert_schedule


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Module-level fixtures so the binding's serialisability
# probe (which runs at add_job time) accepts the callables
# if/when add_job is invoked. Not strictly required for
# this test (no register / no add_job) but keeps the
# fixture shape consistent with the rest of the suite.
# ---------------------------------------------------------------------------


class _MigratedConnFactory:
    """Connection factory pointing at a migrated v2
    SQLite. Each call returns a fresh connection so reader
    + writer roles don't share state."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def __call__(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        runner.apply_pending(conn)
        return conn


_run_counter = {"i": 0}
_evt_counter = {"i": 0}


def _run_id_factory() -> str:
    _run_counter["i"] += 1
    return f"e2e-run-{_run_counter['i']:04d}"


def _evt_id_factory() -> str:
    _evt_counter["i"] += 1
    return f"e2e-evt-{_evt_counter['i']:04d}"


def _fixed_clock() -> datetime:
    return _NOW


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _oneoff_spec(*, schedule_id: str, at: datetime) -> ScheduleSpec:
    return ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="e2e fast oneoff",
        trigger=OneOffTrigger(at_iso_datetime=at, timezone="UTC"),
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
        execution_plan_hash=None,
        authored_at=_NOW.isoformat(),
    ).with_fresh_hash()


def _migrated_factory(tmp_path: Path) -> _MigratedConnFactory:
    db_path = tmp_path / "v2.db"
    primary = sqlite3.connect(str(db_path))
    try:
        runner.apply_pending(primary)
    finally:
        primary.close()
    return _MigratedConnFactory(str(db_path))


# ===========================================================================
# Fast e2e test
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_fast_one_off_walks_full_lifecycle(tmp_path):
    """The load-bearing fast-e2e test.

    Plan section 5.5.a recipe:

      1. Seed an active OneOff with ``at == synthetic_now``.
      2. Build the binding (no ``start()``, no
         ``register()``).
      3. Call ``binding._fire_for(schedule_id)`` -- the
         instance wrapper invokes the module-level
         ``_fire_for`` (sync) with the binding's injected
         callables; wakeup inserts the pending Run +
         run_created event.
      4. Construct a ``Worker`` with the same factories +
         clock; ``await worker.tick()`` once.
      5. Verify: run at status=succeeded; started_at +
         completed_at populated; four events in order
         (run_created, run_claimed, run_started,
         run_succeeded).

    No real wall-clock wait. No APScheduler event loop.
    Wiring is exercised end-to-end through the SAME chain
    APScheduler will fire in production, minus the
    scheduler thread.
    """
    factory = _migrated_factory(tmp_path)

    # 1. Seed an active OneOff at ``now`` so wakeup fires
    # (wakeup fires when ``at <= now``).
    seed_conn = factory()
    try:
        spec = _oneoff_spec(schedule_id="e2e_oneoff", at=_NOW)
        insert_schedule(seed_conn, spec)
        seed_conn.commit()
    finally:
        seed_conn.close()

    # 2. Build the binding. No start / no register -- we
    # invoke the fire path directly.
    binding = SchedulerBinding(
        wakeup_callable=wakeup,
        conn_factory=factory,
        clock=_fixed_clock,
        run_id_factory=_run_id_factory,
        event_id_factory=_evt_id_factory,
        jobstore_url=f"sqlite:///{tmp_path / 'jobs.db'}",
    )

    # 3. Synthesise an APScheduler-fired event by calling
    # the bound _fire_for. This invokes the module-level
    # ``_fire_for`` (sync) which calls wakeup, which
    # inserts the pending Run + run_created event.
    binding._fire_for("e2e_oneoff")

    # Verify wakeup landed a pending Run + run_created.
    verify = factory()
    try:
        runs = verify.execute(
            "SELECT id, status FROM runs WHERE schedule_id = ?",
            ("e2e_oneoff",),
        ).fetchall()
        assert len(runs) == 1
        run_id = runs[0][0]
        assert runs[0][1] == RunStatus.PENDING.value
        events_after_wakeup = verify.execute(
            "SELECT kind FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        assert [e[0] for e in events_after_wakeup] == [
            EventKind.RUN_CREATED.value,
        ]
    finally:
        verify.close()

    # 4. Construct + drive one worker tick.
    worker = Worker(
        conn_factory=factory,
        worker_id="e2e-worker",
        poll_interval=timedelta(milliseconds=10),
        clock=_fixed_clock,
        run_id_factory=_run_id_factory,
        event_id_factory=_evt_id_factory,
    )
    walked = await worker.tick()
    assert walked == run_id

    # 5. Verify the final lifecycle state.
    verify = factory()
    try:
        row = verify.execute(
            "SELECT status, started_at, completed_at FROM runs "
            "WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        status, started_at, completed_at = row
        assert status == RunStatus.SUCCEEDED.value
        assert started_at is not None
        assert completed_at is not None

        # Events in order: run_created (wakeup) ->
        # run_claimed (claim_run) -> run_started (worker
        # transition) -> run_succeeded (worker transition).
        # ORDER BY rowid since all four events share the
        # same ts (fixed clock).
        kinds = verify.execute(
            "SELECT kind FROM events WHERE run_id = ? "
            "ORDER BY rowid ASC",
            (run_id,),
        ).fetchall()
        assert [k[0] for k in kinds] == [
            EventKind.RUN_CREATED.value,
            EventKind.RUN_CLAIMED.value,
            EventKind.RUN_STARTED.value,
            EventKind.RUN_SUCCEEDED.value,
        ]
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_e2e_fast_idle_worker_tick_with_no_pending(tmp_path):
    """Inverse pin: with no Run row in the DB, the worker's
    first tick returns None. Bookends the happy-path test
    above -- proves the chain only fires when the wakeup
    has actually inserted the pending Run."""
    factory = _migrated_factory(tmp_path)

    # No schedule seeded; no wakeup fired.
    worker = Worker(
        conn_factory=factory,
        worker_id="idle-worker",
        poll_interval=timedelta(milliseconds=10),
        clock=_fixed_clock,
        run_id_factory=_run_id_factory,
        event_id_factory=_evt_id_factory,
    )
    walked = await worker.tick()
    assert walked is None
    # And no rows landed.
    verify = factory()
    try:
        runs = verify.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        events = verify.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert runs == 0
        assert events == 0
    finally:
        verify.close()
