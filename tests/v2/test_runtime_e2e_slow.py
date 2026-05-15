"""Slow end-to-end integration test for the v2 runtime.

Plan section 5.5.b -- ``@pytest.mark.slow``, excluded from
the required fast CI lane via ``pytest -m "not slow"``.

Exercises the FULL APScheduler event-loop integration. Real
``AsyncIOScheduler`` is constructed via ``boot_runtime``;
real production clock + id factories flow through; the
binding's persisted ``DateTrigger`` fires after a
wall-clock wait; the module-level ``_fire_for`` runs as
APScheduler's callback; wakeup inserts the pending Run; the
worker picks it up via its poll loop; the lifecycle walks
``pending -> claimed -> running -> succeeded``.

Recipe:

  1. Seed an active OneOff with
     ``at_iso_datetime = now + 4 s``. The 4 s buffer is
     generous enough that the OneOff is NOT past-due at
     ``boot_runtime`` 's clock() read (which would let the
     boot backfill insert the Run instead of APScheduler's
     wall-clock fire path -- defeating the test). The
     immediate post-boot "no Run row yet" assertion below
     pins this: if backfill ever created the row, the
     assertion fails before the wait begins.
  2. ``await boot_runtime(...)`` with a short
     ``poll_interval`` so the worker reacts quickly once
     wakeup lands.
  3. Assert no Run row exists immediately after boot --
     confirms the row, when it appears, came from
     APScheduler's wall-clock fire (the DateTrigger ->
     module-level ``_fire_for`` -> wakeup chain), not from
     boot backfill.
  4. Wait (poll + small sleep) until the run row reaches
     ``succeeded`` OR a 12-second wall-clock budget elapses.
  5. Assert lifecycle complete: status=succeeded,
     started_at + completed_at populated, 4 events in the
     expected order.
  6. ``await asyncio.wait_for(shutdown_runtime(handle),
     timeout=5.0)`` -- a hard ceiling so a leaked
     AsyncIOScheduler thread / engine pool fails loud
     instead of hanging the test session.

The fast counterpart in ``test_runtime_e2e_fast.py`` is
what guarantees PR-time confidence in the wiring; this
test is the periodic check that APScheduler's event-loop
integration actually fires our job on wall-clock time. Per
plan section 7 acceptance item 13, NOT gating on PRs.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
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
from app.v2.runtime.boot import boot_runtime, shutdown_runtime
from app.v2.storage.schedules import insert_schedule


pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Module-level fixtures. Counter-based id factories satisfy
# the binding's serialisability probe at register time --
# module-level callables + class instances round-trip
# cleanly; lambdas / closures do not. The clock stays as
# production ``prod_clock`` flowed through ``boot_runtime``'s
# default kwargs.
# ---------------------------------------------------------------------------


class _MigratedConnFactory:
    """Connection factory pointing at a migrated v2 SQLite.
    Each call returns a fresh connection so reader + writer
    roles don't share state. Class instance instead of a
    closure so the binding's serialisability probe accepts
    the factory."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def __call__(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        runner.apply_pending(conn)
        return conn


class _RunIdFactory:
    """Counter-based run id factory. Module-level class
    (round-trippable via the SQLAlchemy job store) with
    per-instance state."""

    def __init__(self) -> None:
        self.i = 0

    def __call__(self) -> str:
        self.i += 1
        return f"slow-e2e-run-{self.i:04d}"


class _EventIdFactory:
    def __init__(self) -> None:
        self.i = 0

    def __call__(self) -> str:
        self.i += 1
        return f"slow-e2e-evt-{self.i:04d}"


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
        description="slow e2e oneoff",
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
        authored_at=at.isoformat(),
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
# Slow e2e -- real AsyncIOScheduler wall-clock fire
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_slow_one_off_fires_on_real_wall_clock(tmp_path):
    """End-to-end test against the real APScheduler event
    loop. Seeds an active OneOff 4 s in the future; boots the
    runtime; pins that boot backfill did NOT insert the row;
    waits up to 12 s for the run row to reach ``succeeded``;
    asserts the full 4-event ledger and the lifecycle
    timestamps; shuts down cleanly under a hard 5 s timeout.

    Why 4 s + 12 s budget: a comfortable margin over the
    measured boot cost on this box (well under 1 s), so
    ``boot_runtime`` 's ``clock()`` read stays earlier than
    ``fire_at`` -- the boot backfill skips the schedule
    (``fire_at > now``), and only APScheduler's wall-clock
    timer fires the persisted ``DateTrigger`` later. The
    12 s budget covers fire-time + worker poll (100 ms) +
    state transitions with margin. The actual fire is
    single-shot on APScheduler's wall-clock timer.
    """
    factory = _migrated_factory(tmp_path)

    fire_at = datetime.now(timezone.utc) + timedelta(seconds=4)
    spec = _oneoff_spec(schedule_id="slow_e2e", at=fire_at)
    seed_conn = factory()
    try:
        insert_schedule(seed_conn, spec)
        seed_conn.commit()
    finally:
        seed_conn.close()

    handle = await boot_runtime(
        factory,
        jobstore_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        poll_interval=timedelta(milliseconds=100),
        run_id_factory=_RunIdFactory(),
        event_id_factory=_EventIdFactory(),
    )

    try:
        # Pin: no Run row right after boot. Confirms the row
        # that appears later came from APScheduler's
        # wall-clock fire path, not from boot backfill. If
        # this trips, ``fire_at`` was inside the backfill
        # window at boot time -- bump it OR investigate why
        # boot took unexpectedly long.
        post_boot = factory()
        try:
            existing = post_boot.execute(
                "SELECT COUNT(*) FROM runs "
                "WHERE schedule_id = ?",
                ("slow_e2e",),
            ).fetchone()[0]
            assert existing == 0, (
                "Boot backfill inserted a Run before "
                "APScheduler could fire on wall clock. Push "
                "fire_at farther out so this test exercises "
                "the wall-clock fire path, not the backfill."
            )
        finally:
            post_boot.close()

        # Poll until the run row reaches succeeded OR the
        # 12 s wall-clock budget elapses.
        deadline = time.monotonic() + 12.0
        terminal_row = None
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            probe = factory()
            try:
                row = probe.execute(
                    "SELECT id, status FROM runs "
                    "WHERE schedule_id = ?",
                    ("slow_e2e",),
                ).fetchone()
            finally:
                probe.close()
            if row is not None and row[1] == RunStatus.SUCCEEDED.value:
                terminal_row = row
                break

        assert terminal_row is not None, (
            "OneOff did not reach succeeded within the 12 s "
            "wall-clock budget. APScheduler did not fire OR "
            "the worker did not walk the lifecycle."
        )
        run_id = terminal_row[0]

        verify = factory()
        try:
            lifecycle = verify.execute(
                "SELECT status, started_at, completed_at "
                "FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            assert lifecycle is not None
            status, started_at, completed_at = lifecycle
            assert status == RunStatus.SUCCEEDED.value
            assert started_at is not None
            assert completed_at is not None

            # Ledger order: run_created (wakeup) -> run_claimed
            # (claim_run) -> run_started (worker transition) ->
            # run_succeeded (worker transition). ORDER BY rowid
            # because events at the same ``ts`` are ordered by
            # insertion order; under real-time the four
            # transitions can land within the same ts bucket
            # (sub-millisecond) so ts-based ORDER is
            # nondeterministic.
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
    finally:
        # Tear the runtime down under a HARD timeout so a
        # leaked AsyncIOScheduler thread or jobstore engine
        # pool fails loud instead of hanging the test session.
        # 5 s is generous -- AsyncIOScheduler's
        # shutdown(wait=False) + a SQLAlchemy engine dispose
        # finish in milliseconds on a healthy run.
        await asyncio.wait_for(shutdown_runtime(handle), timeout=5.0)
        # Structural pin (reviewer's slice-7b regression): if
        # AsyncIOScheduler's deferred ``_shutdown`` never
        # actually executed -- e.g. binding.stop returned
        # after a single asyncio.sleep(0) before the
        # ``call_soon_threadsafe`` callback ran -- the
        # SQLAlchemyJobStore engine pool stays undisposed and
        # the pytest process hangs at interpreter exit
        # waiting on the leaked resources. Asserting
        # ``running is False`` here catches that regression
        # structurally inside the test rather than via an
        # external timeout on the harness.
        assert handle.binding._scheduler.running is False, (
            "AsyncIOScheduler still reports running=True "
            "after shutdown_runtime returned. The deferred "
            "_shutdown did not execute; SQLAlchemyJobStore "
            "engine + sqlite handles are leaked and the "
            "process will hang at interpreter exit."
        )
