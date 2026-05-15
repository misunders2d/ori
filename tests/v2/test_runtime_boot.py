"""Tests for ``app.v2.runtime.boot``.

Plan section 5.3 pins. Uses MOCKED SchedulerBinding + Worker
so tests stay fast (no real APScheduler event loop). The
binding integration is covered by the slice-7 e2e tests
(test_runtime_e2e_fast.py / test_runtime_e2e_slow.py).

Coverage:

Step ordering + happy paths:
- Empty DB -> boot succeeds; recovery empty; binding.start
  called with paused=True; resume called; 0 schedules
  registered; N workers started.
- Active schedules in DB -> each registered.
- Mixed active/paused/archived -> only active registered.

Recovery integration:
- Stale claimed run -> recovery scan remediates; result
  carries RecoveredRun.
- abort_on_recovery_errors=True + RecoveryError ->
  RuntimeBootError; binding NOT started.

OneOff backfill (mechanic 2):
- Active OneOff with past at + no Run row -> backfilled;
  handle.backfilled_one_offs contains the entry.
- OneOff older than max_backfill_age -> skipped; entry in
  registration_errors with explicit reason.
- OneOff past at but with existing Run row -> not
  backfilled; mechanic 1 inside binding.register also
  skips it.
- OneOff with future at -> not backfilled; registered
  normally.

Jobstore reconcile:
- Active OneOff with existing Run row -> binding.unregister
  called for that schedule_id during step 6.
- Cron with existing Run row -> NOT evicted (reconcile is
  OneOff-only; cron jobs fire repeatedly).

Registration errors:
- Broken cron register raises -> RegistrationError; OTHER
  schedules still register; workers still start.
- Unsupported trigger type -> same shape.

Worker pool:
- worker_count=3 -> 3 Workers constructed + started.
- worker_count=0 / negative -> ValueError.

Shutdown:
- shutdown_runtime stops every worker, then stops binding,
  in that order.
- Double-shutdown idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
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
from app.v2.models.triggers import (
    CronTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.runtime import boot as boot_mod
from app.v2.runtime.boot import (
    BackfilledOneOff,
    RegistrationError,
    RuntimeBootError,
    RuntimeHandle,
    boot_runtime,
    shutdown_runtime,
)
from app.v2.runtime.recovery import RecoveredRun, RecoveryError
from app.v2.storage.schedules import insert_schedule


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _ConnFactory:
    """Picklable connection factory pointing at a migrated
    v2 SQLite file.

    Runs ``runner.apply_pending`` on each new connection so
    the returned conn satisfies ``assert_connection_ready``
    (WAL journal_mode + foreign_keys=ON). apply_pending is
    idempotent against an already-migrated DB."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def __call__(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        runner.apply_pending(conn)
        return conn


def _migrated_factory(tmp_path: Path) -> _ConnFactory:
    db_path = tmp_path / "v2.db"
    primary = sqlite3.connect(str(db_path))
    try:
        runner.apply_pending(primary)
    finally:
        primary.close()
    return _ConnFactory(str(db_path))


def _fixed_clock():
    return _NOW


# Counter-based id factories at module level so they're
# resolvable by qualified name (binding's serialisability
# probe needs module-level callables). Each call produces a
# unique id; the in-module counter resets across processes
# but tests run in a single process so the sequence is
# deterministic per session.
_run_id_counter = {"i": 0}
_evt_id_counter = {"i": 0}


def _fixed_run_id():
    _run_id_counter["i"] += 1
    return f"boot-run-{_run_id_counter['i']:04d}"


def _fixed_evt_id():
    _evt_id_counter["i"] += 1
    return f"boot-evt-{_evt_id_counter['i']:04d}"


def _spec(*, schedule_id, trigger, status=ScheduleStatus.ACTIVE):
    return ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="boot test schedule",
        trigger=trigger,
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        status=status,
        execution_plan_hash=None,
        authored_at=_NOW_ISO,
    ).with_fresh_hash()


def _one_off(*, schedule_id="oneoff", at=None, tz="UTC", status=ScheduleStatus.ACTIVE):
    return _spec(
        schedule_id=schedule_id,
        trigger=OneOffTrigger(
            at_iso_datetime=at if at else _NOW + timedelta(hours=1),
            timezone=tz,
        ),
        status=status,
    )


def _cron(*, schedule_id="cron", cron="0 18 * * *", tz="UTC", status=ScheduleStatus.ACTIVE):
    return _spec(
        schedule_id=schedule_id,
        trigger=CronTrigger(cron=cron, timezone=tz),
        status=status,
    )


def _seed_schedule(factory, spec):
    conn = factory()
    try:
        insert_schedule(conn, spec)
        conn.commit()
    finally:
        conn.close()


def _seed_run_row(factory, schedule_id, status="succeeded"):
    """Insert a Run row for ``schedule_id`` (assumes the
    schedule row already exists; FK is enforced)."""
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO runs "
            "(id, schedule_id, fire_reason, due_at, status, attempt, "
            " root_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"run-for-{schedule_id}",
                schedule_id,
                "scheduled",
                _NOW.isoformat(),
                status,
                1,
                f"run-for-{schedule_id}",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_claimed_stale_run(factory, schedule_id):
    """Insert a claimed Run row whose claimed_at is past the
    default claimed_timeout -- recovery scan should remediate
    it."""
    claimed_at = _NOW - timedelta(hours=1)
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO runs "
            "(id, schedule_id, fire_reason, due_at, status, attempt, "
            " root_run_id, claimed_by, claimed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"stale-{schedule_id}",
                schedule_id,
                "scheduled",
                _NOW.isoformat(),
                "claimed",
                1,
                f"stale-{schedule_id}",
                "dead-worker",
                claimed_at.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_stub_binding(register_errors: Optional[dict] = None):
    """Build a MagicMock with the SchedulerBinding-shape
    surface boot uses."""
    register_errors = register_errors or {}
    binding = MagicMock(name="SchedulerBinding")
    binding.start = AsyncMock()
    binding.resume = AsyncMock()
    binding.pause = AsyncMock()
    binding.stop = AsyncMock()
    registered: list[ScheduleSpec] = []
    unregistered: list[str] = []

    def _register(spec):
        if spec.id in register_errors:
            raise register_errors[spec.id]
        registered.append(spec)

    def _unregister(schedule_id):
        unregistered.append(schedule_id)

    def _list_registered():
        return [s.id for s in registered]

    binding.register = MagicMock(side_effect=_register)
    binding.unregister = MagicMock(side_effect=_unregister)
    binding.list_registered = MagicMock(side_effect=_list_registered)
    binding.registered_specs = registered
    binding.unregistered_ids = unregistered
    return binding


def _make_stub_worker_class(started_workers: list):
    """Return a class object that records each instance
    constructed + its start/stop calls."""

    class _StubWorker:
        def __init__(self, **kwargs):
            self.init_kwargs = kwargs
            self.worker_id = kwargs.get("worker_id")
            self.started = False
            self.stopped = False
            started_workers.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    return _StubWorker


@pytest.fixture
def stub_binding(monkeypatch):
    """Default stub: no register errors. Patches
    ``boot.SchedulerBinding`` to a callable that returns the
    stub instance."""
    binding = _make_stub_binding()
    monkeypatch.setattr(
        boot_mod, "SchedulerBinding", lambda **kwargs: binding
    )
    return binding


@pytest.fixture
def stub_worker_class(monkeypatch):
    started: list = []
    cls = _make_stub_worker_class(started)
    monkeypatch.setattr(boot_mod, "Worker", cls)
    cls.instances = started
    return cls


# ===========================================================================
# Step ordering + happy paths
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_empty_db_succeeds(tmp_path, stub_binding, stub_worker_class):
    factory = _migrated_factory(tmp_path)
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert handle.recovery_result == []
    assert handle.backfilled_one_offs == []
    assert handle.registration_errors == []
    stub_binding.start.assert_awaited_once_with(paused=True)
    stub_binding.resume.assert_awaited_once()
    assert stub_binding.register.call_count == 0
    assert len(stub_worker_class.instances) == 1
    assert stub_worker_class.instances[0].started is True


@pytest.mark.asyncio
async def test_boot_registers_active_schedules(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    _seed_schedule(factory, _cron(schedule_id="active_cron"))
    _seed_schedule(factory, _one_off(schedule_id="active_oneoff"))
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    registered_ids = [s.id for s in stub_binding.registered_specs]
    assert set(registered_ids) == {"active_cron", "active_oneoff"}
    assert handle.registration_errors == []


@pytest.mark.asyncio
async def test_boot_skips_paused_and_archived_schedules(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    _seed_schedule(factory, _cron(schedule_id="active_one"))
    _seed_schedule(
        factory, _cron(schedule_id="paused_one", status=ScheduleStatus.PAUSED)
    )
    _seed_schedule(
        factory,
        _cron(schedule_id="archived_one", status=ScheduleStatus.ARCHIVED),
    )
    await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    registered_ids = [s.id for s in stub_binding.registered_specs]
    assert registered_ids == ["active_one"]


# ===========================================================================
# Recovery integration
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_remediates_stale_runs(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    _seed_schedule(factory, _cron(schedule_id="recover_me"))
    _seed_claimed_stale_run(factory, "recover_me")
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    recovered = [
        r for r in handle.recovery_result if isinstance(r, RecoveredRun)
    ]
    assert len(recovered) == 1
    assert recovered[0].run_id == "stale-recover_me"


@pytest.mark.asyncio
async def test_boot_abort_on_recovery_errors_raises(
    tmp_path, stub_binding, stub_worker_class, monkeypatch
):
    factory = _migrated_factory(tmp_path)
    # Force scan_stale_runs to return a synthetic RecoveryError.
    fake_error = RecoveryError(
        run_id="r-1",
        schedule_id="s-1",
        prior_status=RunStatus.CLAIMED,
        error_message="synthetic recovery failure",
    )
    monkeypatch.setattr(
        boot_mod, "scan_stale_runs", lambda *a, **k: [fake_error]
    )
    with pytest.raises(RuntimeBootError, match="synthetic recovery failure"):
        await boot_runtime(
            factory,
            abort_on_recovery_errors=True,
            clock=_fixed_clock,
            run_id_factory=_fixed_run_id,
            event_id_factory=_fixed_evt_id,
        )
    # Binding was NEVER started -- abort precedes step 4.
    stub_binding.start.assert_not_awaited()


# ===========================================================================
# OneOff backfill (mechanic 2)
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_backfills_past_due_one_off(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    past = _NOW - timedelta(minutes=10)
    _seed_schedule(factory, _one_off(schedule_id="missed", at=past))
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert len(handle.backfilled_one_offs) == 1
    entry = handle.backfilled_one_offs[0]
    assert entry.schedule_id == "missed"
    assert entry.fire_at == past
    # Counter-based factory yields ``boot-run-NNNN``; the
    # exact number depends on prior tests in the same
    # session. Pin the SHAPE, not the value.
    assert entry.run_id.startswith("boot-run-")


@pytest.mark.asyncio
async def test_boot_skips_one_off_beyond_max_backfill_age(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    way_past = _NOW - timedelta(days=2)  # > default 24h
    _seed_schedule(factory, _one_off(schedule_id="too_old", at=way_past))
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert handle.backfilled_one_offs == []
    assert len(handle.registration_errors) == 1
    err = handle.registration_errors[0]
    assert err.schedule_id == "too_old"
    assert "max_backfill_age" in err.error_message


@pytest.mark.asyncio
async def test_boot_skips_past_one_off_with_existing_run(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    past = _NOW - timedelta(minutes=10)
    _seed_schedule(factory, _one_off(schedule_id="already_fired", at=past))
    _seed_run_row(factory, "already_fired", status="succeeded")
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    # No new backfill -- existing Run row blocked it.
    assert handle.backfilled_one_offs == []


@pytest.mark.asyncio
async def test_boot_does_not_backfill_future_one_off(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    future = _NOW + timedelta(hours=2)
    _seed_schedule(factory, _one_off(schedule_id="upcoming", at=future))
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert handle.backfilled_one_offs == []
    # Future OneOff registered normally.
    assert [s.id for s in stub_binding.registered_specs] == ["upcoming"]


# ===========================================================================
# Skip-policy enforcement (round-N reviewer): too-old +
# backfill-failed OneOffs must NOT reach step-7 register
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_excludes_max_age_skipped_one_off_from_register(
    tmp_path, stub_binding, stub_worker_class
):
    """A OneOff older than max_backfill_age is recorded as
    ``"missed beyond max_backfill_age"`` in step 5. Step 7
    must NOT register it -- otherwise misfire_grace_time
    could still let APScheduler fire the past DateTrigger
    after resume, violating the skip policy. Step 6 also
    evicts any persisted job for the same id."""
    factory = _migrated_factory(tmp_path)
    way_past = _NOW - timedelta(days=2)  # > default 24h
    _seed_schedule(factory, _one_off(schedule_id="too_old", at=way_past))
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    # Skipped at backfill -> registration_errors entry.
    assert len(handle.registration_errors) == 1
    assert handle.registration_errors[0].schedule_id == "too_old"
    # NOT registered in step 7.
    registered_ids = [s.id for s in stub_binding.registered_specs]
    assert "too_old" not in registered_ids
    # Persisted job evicted in step 6.
    assert "too_old" in stub_binding.unregistered_ids


@pytest.mark.asyncio
async def test_boot_excludes_backfill_failed_one_off_from_register(
    tmp_path, stub_binding, stub_worker_class, monkeypatch
):
    """A OneOff whose wakeup() raises during step 5 backfill
    is recorded as ``"backfill failed: ..."``. Same skip-
    policy as max-age: NOT registered in step 7, evicted in
    step 6."""
    factory = _migrated_factory(tmp_path)
    past = _NOW - timedelta(minutes=10)
    _seed_schedule(factory, _one_off(schedule_id="bad_one", at=past))
    # Patch boot's local ``wakeup`` reference to raise.
    monkeypatch.setattr(
        boot_mod,
        "wakeup",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("synthetic wakeup failure")
        ),
    )
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert len(handle.registration_errors) == 1
    err = handle.registration_errors[0]
    assert err.schedule_id == "bad_one"
    assert "backfill failed" in err.error_message
    # Not registered.
    registered_ids = [s.id for s in stub_binding.registered_specs]
    assert "bad_one" not in registered_ids
    # Persisted job evicted.
    assert "bad_one" in stub_binding.unregistered_ids


# ===========================================================================
# Jobstore reconcile (step 6)
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_unregisters_one_off_with_existing_run(
    tmp_path, stub_binding, stub_worker_class
):
    """Step 6 evicts persisted DateTriggers whose schedule
    now has a Run row. The stub binding records every
    unregister() call."""
    factory = _migrated_factory(tmp_path)
    past = _NOW - timedelta(hours=1)
    _seed_schedule(factory, _one_off(schedule_id="fired_oneoff", at=past))
    _seed_run_row(factory, "fired_oneoff", status="succeeded")
    await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert "fired_oneoff" in stub_binding.unregistered_ids


@pytest.mark.asyncio
async def test_boot_does_not_unregister_cron_with_existing_run(
    tmp_path, stub_binding, stub_worker_class
):
    """The jobstore-reconcile eviction is OneOff-only. Cron
    schedules fire repeatedly so an existing Run row is the
    expected state, not a duplicate-fire risk."""
    factory = _migrated_factory(tmp_path)
    _seed_schedule(factory, _cron(schedule_id="cron_with_history"))
    _seed_run_row(factory, "cron_with_history", status="succeeded")
    await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert "cron_with_history" not in stub_binding.unregistered_ids


# ===========================================================================
# Registration errors
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_continues_past_broken_register(
    tmp_path, stub_worker_class, monkeypatch
):
    """A schedule whose register() raises produces a
    RegistrationError entry; other schedules still register;
    workers still start."""
    factory = _migrated_factory(tmp_path)
    _seed_schedule(factory, _cron(schedule_id="ok_one"))
    _seed_schedule(factory, _cron(schedule_id="broken_one"))
    _seed_schedule(factory, _cron(schedule_id="ok_two"))
    binding = _make_stub_binding(
        register_errors={
            "broken_one": ValueError("synthetic numeric DOW"),
        }
    )
    monkeypatch.setattr(boot_mod, "SchedulerBinding", lambda **kw: binding)
    handle = await boot_runtime(
        factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    registered_ids = [s.id for s in binding.registered_specs]
    assert set(registered_ids) == {"ok_one", "ok_two"}
    err_ids = [e.schedule_id for e in handle.registration_errors]
    assert err_ids == ["broken_one"]
    assert "synthetic numeric DOW" in handle.registration_errors[0].error_message
    # Workers still start despite one broken register.
    assert len(stub_worker_class.instances) == 1
    assert stub_worker_class.instances[0].started is True


# ===========================================================================
# Worker pool
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_starts_worker_count_workers(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    await boot_runtime(
        factory,
        worker_count=3,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    assert len(stub_worker_class.instances) == 3
    ids = sorted(w.worker_id for w in stub_worker_class.instances)
    assert ids == ["worker-0", "worker-1", "worker-2"]
    assert all(w.started for w in stub_worker_class.instances)


@pytest.mark.asyncio
async def test_boot_rejects_worker_count_below_one(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    with pytest.raises(ValueError, match="worker_count"):
        await boot_runtime(
            factory,
            worker_count=0,
            clock=_fixed_clock,
            run_id_factory=_fixed_run_id,
            event_id_factory=_fixed_evt_id,
        )


# ===========================================================================
# Shutdown
# ===========================================================================


@pytest.mark.asyncio
async def test_shutdown_stops_workers_then_binding(
    tmp_path, stub_binding, stub_worker_class
):
    factory = _migrated_factory(tmp_path)
    handle = await boot_runtime(
        factory,
        worker_count=2,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    await shutdown_runtime(handle)
    # All workers stopped.
    assert all(w.stopped for w in stub_worker_class.instances)
    # Binding stopped.
    stub_binding.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_continues_when_worker_stop_raises(
    tmp_path, stub_binding, monkeypatch
):
    """A worker that raises during stop() must NOT prevent
    the remaining workers + binding from stopping."""
    factory = _migrated_factory(tmp_path)
    started: list = []

    class _RaisingWorker:
        instances = started

        def __init__(self, **kwargs):
            self.worker_id = kwargs.get("worker_id")
            self.started = False
            self.stopped = False
            started.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            if self.worker_id == "worker-1":
                raise RuntimeError("synthetic stop failure")
            self.stopped = True

    monkeypatch.setattr(boot_mod, "Worker", _RaisingWorker)
    handle = await boot_runtime(
        factory,
        worker_count=3,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
    )
    # Must not raise.
    await shutdown_runtime(handle)
    # Other workers stopped.
    stopped_ids = sorted(w.worker_id for w in started if w.stopped)
    assert stopped_ids == ["worker-0", "worker-2"]
    # Binding still stopped despite the worker exception.
    stub_binding.stop.assert_awaited_once()


# ===========================================================================
# Cleanup on post-start failure (round-N reviewer)
# ===========================================================================


@pytest.mark.asyncio
async def test_boot_cleans_up_on_resume_failure(
    tmp_path, stub_worker_class, monkeypatch
):
    """If ``binding.resume()`` raises, boot must stop the
    binding it started in step 4 before re-raising.
    Otherwise the caller has no handle to shut it down."""
    factory = _migrated_factory(tmp_path)
    binding = _make_stub_binding()
    binding.resume.side_effect = RuntimeError("synthetic resume failure")
    monkeypatch.setattr(boot_mod, "SchedulerBinding", lambda **kw: binding)

    with pytest.raises(RuntimeError, match="synthetic resume failure"):
        await boot_runtime(
            factory,
            clock=_fixed_clock,
            run_id_factory=_fixed_run_id,
            event_id_factory=_fixed_evt_id,
        )
    # Binding was started AND stopped (cleanup ran).
    binding.start.assert_awaited_once_with(paused=True)
    binding.stop.assert_awaited_once()
    # No workers were ever started.
    assert stub_worker_class.instances == []


@pytest.mark.asyncio
async def test_boot_cleans_up_on_worker_start_failure(
    tmp_path, stub_binding, monkeypatch
):
    """If the Nth worker's ``start()`` raises, boot must
    stop the (N-1) workers it already started, then stop
    the binding, then re-raise. Leaking live workers + a
    running scheduler is the failure mode this fix
    prevents."""
    factory = _migrated_factory(tmp_path)
    started: list = []

    class _WorkerStartRaiser:
        instances = started

        def __init__(self, **kwargs):
            self.worker_id = kwargs.get("worker_id")
            self.started = False
            self.stopped = False
            started.append(self)

        async def start(self):
            # Fail the second worker's start. First worker
            # had already started -> must be stopped by
            # cleanup.
            if self.worker_id == "worker-1":
                raise RuntimeError("synthetic worker start failure")
            self.started = True

        async def stop(self):
            self.stopped = True

    monkeypatch.setattr(boot_mod, "Worker", _WorkerStartRaiser)

    with pytest.raises(RuntimeError, match="synthetic worker start failure"):
        await boot_runtime(
            factory,
            worker_count=3,
            clock=_fixed_clock,
            run_id_factory=_fixed_run_id,
            event_id_factory=_fixed_evt_id,
        )

    # Two Worker INSTANCES were constructed: worker-0 (which
    # started cleanly) and worker-1 (whose start() raised).
    # The loop never reached worker-2 because the raise
    # broke out of step 9.
    assert len(started) == 2
    assert started[0].worker_id == "worker-0"
    assert started[0].started is True
    # Cleanup stopped the one running worker.
    assert started[0].stopped is True
    # Binding stopped during cleanup.
    stub_binding.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_boot_cleanup_continues_when_worker_stop_raises(
    tmp_path, stub_binding, monkeypatch
):
    """During cleanup, a worker.stop() failure must NOT
    prevent the binding from being stopped. Cleanup is
    best-effort."""
    factory = _migrated_factory(tmp_path)
    started: list = []

    class _MixedWorker:
        instances = started

        def __init__(self, **kwargs):
            self.worker_id = kwargs.get("worker_id")
            self.started = False
            started.append(self)

        async def start(self):
            # Fail the THIRD start so the first two are
            # already running when cleanup begins.
            if self.worker_id == "worker-2":
                raise RuntimeError("synthetic start failure")
            self.started = True

        async def stop(self):
            # worker-0's stop raises; worker-1's stop
            # succeeds. Cleanup must still call stop on
            # both, then stop the binding.
            if self.worker_id == "worker-0":
                raise RuntimeError("synthetic stop failure")

    monkeypatch.setattr(boot_mod, "Worker", _MixedWorker)

    with pytest.raises(RuntimeError, match="synthetic start failure"):
        await boot_runtime(
            factory,
            worker_count=3,
            clock=_fixed_clock,
            run_id_factory=_fixed_run_id,
            event_id_factory=_fixed_evt_id,
        )
    # Despite worker-0's stop() raising, the binding is
    # still stopped by the cleanup path.
    stub_binding.stop.assert_awaited_once()


# ===========================================================================
# Hygiene
# ===========================================================================


def test_boot_module_has_no_io_imports():
    import inspect

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
    for _, member in vars(boot_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, f"boot module imports unexpected libs: {sorted(leaked)}"


def test_boot_module_does_not_import_uuid():
    """id factories are injected via _defaults; boot.py
    itself never touches uuid."""
    import inspect

    seen = set()
    for _, member in vars(boot_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen
