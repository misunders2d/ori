"""Tests for ``app.v2.runtime.binding`` -- slice 2 (lifecycle).

Pins per ``docs/PHASE_5_PLAN.md`` section 3.2 + section 5.2.

Slice 2 owns:
- Construction + injection validation.
- Lifecycle (start / stop / pause / resume / is_paused) +
  the documented paused-start / resume flow.
- ``_fire_for`` exception-swallow skeleton.

Registration (``register`` / ``unregister`` / ``reregister``)
is slice 3+ -- not exercised here. The boot sequence
(``boot_runtime``) is slice 5.

Coverage:

Construction:
- All defaults succeed.
- Custom jobstore_url succeeds.
- Empty jobstore_url -> ValueError.
- Negative misfire_grace_time -> ValueError.
- Zero misfire_grace_time rejected (APScheduler requires
  positive int or None); None accepted as "no expiry".
- Non-callable wakeup_callable / conn_factory / clock /
  run_id_factory / event_id_factory -> TypeError naming the
  offending kwarg.

Lifecycle:
- ``is_paused()`` returns False before ``start()``.
- ``start()`` then ``is_paused()`` False.
- ``start(paused=True)`` then ``is_paused()`` True.
- ``start()`` twice -> RuntimeError.
- ``stop()`` before ``start()`` is a no-op.
- ``stop()`` after ``start()`` clears the started flag;
  ``is_paused()`` False after.
- Double ``stop()`` is a no-op.
- ``await stop()`` yields the event loop so APScheduler's
  deferred ``_shutdown`` task lands before returning -- pin
  via observing scheduler.state transitions through 0.
- ``pause()`` before ``start()`` -> RuntimeError.
- ``resume()`` before ``start()`` -> RuntimeError.
- ``pause()`` after running -> ``is_paused()`` True.
- ``resume()`` after pause -> ``is_paused()`` False.
- Double pause + double resume are idempotent.
- ``start(paused=True)`` then await ``resume()`` ->
  ``is_paused()`` False.

APScheduler callback ``_fire_for``:
- Happy path: invokes wakeup with the documented kwargs;
  passes the clock's return value as ``now``; opens + closes
  the conn via ``conn_factory``.
- ``_fire_for`` swallows ``Exception`` raised by the wakeup;
  logs via ``logger.exception``; does NOT re-raise. The
  conn from ``conn_factory`` is still closed.
- ``_fire_for`` propagates ``BaseException`` (synthesises
  ``CancelledError``) so APScheduler's shutdown path sees
  the cancel signal. The conn is still closed.

Hygiene smoke:
- Module imports apscheduler (allowed) but no I/O libs.
- Module does not import uuid.
- Module exposes no reasoning / emit / delegate / transfer
  / sub_agent / dispatch / invoke public callable.
- ``binding._fire_for`` source contains the
  ``except Exception`` line (not ``except BaseException``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

# Reach the binding MODULE via sys.modules. The package
# __init__.py re-exports SchedulerBinding under the name
# ``SchedulerBinding`` (not ``binding``) so there is no
# attribute-shadow on the submodule -- but using
# sys.modules keeps the pattern uniform with the wakeup
# hygiene tests where shadowing IS a concern.
import app.v2.runtime  # noqa: F401 -- trigger package import

binding_mod = sys.modules["app.v2.runtime.binding"]

from apscheduler.triggers.cron import (
    CronTrigger as APSchedulerCronTrigger,
)
from apscheduler.triggers.date import DateTrigger as APSchedulerDateTrigger

from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.runtime.binding import SchedulerBinding


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _jobstore_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'jobs.db'}"


# Module-level callables (not lambdas) so the binding state
# is serialisable. APScheduler's SQLAlchemy job store
# serialises the registered ``func`` -- a bound method on a
# SchedulerBinding -- which transitively serialises the
# binding's instance dict. Lambda-bound closures defined
# inside ``_make_binding`` fail with ``Can't get local
# object``; module-level functions resolve cleanly.


def _noop_wakeup(conn, **kwargs):
    """Default wakeup_callable: returns empty list."""
    return []


def _memory_conn_factory():
    return sqlite3.connect(":memory:")


def _fixed_clock():
    return _NOW


def _fixed_run_id():
    return "run-x"


def _fixed_evt_id():
    return "evt-x"


def _make_binding(tmp_path, **overrides) -> SchedulerBinding:
    """Construct a binding with sensible test defaults.
    Overrides replace individual kwargs.

    Uses module-level callables so the binding instance is
    serialisable by the SQLAlchemy job store."""
    kwargs = dict(
        wakeup_callable=_noop_wakeup,
        conn_factory=_memory_conn_factory,
        clock=_fixed_clock,
        run_id_factory=_fixed_run_id,
        event_id_factory=_fixed_evt_id,
        jobstore_url=_jobstore_url(tmp_path),
    )
    kwargs.update(overrides)
    return SchedulerBinding(**kwargs)


def _spec(
    *,
    schedule_id: str = "sched_test",
    trigger,
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> ScheduleSpec:
    """Build a frozen ScheduleSpec around the given trigger."""
    return ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="binding test schedule",
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


def _one_off_spec(*, schedule_id="oneoff_test", at=None, tz="UTC"):
    return _spec(
        schedule_id=schedule_id,
        trigger=OneOffTrigger(
            at_iso_datetime=at if at is not None else _NOW + timedelta(hours=1),
            timezone=tz,
        ),
    )


def _cron_spec(*, schedule_id="cron_test", cron="0 18 * * *", tz="UTC"):
    return _spec(
        schedule_id=schedule_id,
        trigger=CronTrigger(cron=cron, timezone=tz),
    )


# ===========================================================================
# Construction
# ===========================================================================


def test_construction_with_defaults_succeeds(tmp_path):
    """All defaults except the two required positionals +
    a tmp jobstore url. Should succeed without error."""
    b = SchedulerBinding(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: sqlite3.connect(":memory:"),
        jobstore_url=_jobstore_url(tmp_path),
    )
    assert b.is_paused() is False


def test_construction_with_explicit_jobstore_url(tmp_path):
    b = _make_binding(tmp_path)
    assert b is not None


def test_construction_with_none_misfire_grace_time(tmp_path):
    """None is accepted -- it flows into APScheduler's
    ``job_defaults`` as 'no expiry' (always fire, even
    late). Pin so a future regression that rejects None
    breaks here."""
    b = _make_binding(tmp_path, misfire_grace_time=None)
    assert b._misfire_grace_time is None


def test_empty_jobstore_url_rejected(tmp_path):
    with pytest.raises(ValueError, match="jobstore_url"):
        _make_binding(tmp_path, jobstore_url="")


@pytest.mark.parametrize("bad", [0, -1, -3600])
def test_zero_or_negative_misfire_grace_time_rejected(tmp_path, bad):
    """APScheduler 3.x's ``Job`` class rejects
    ``misfire_grace_time=0`` (probed: "must be either None
    or a positive integer"). Negative is nonsense. Mirror
    the contract at construction so the failure surfaces
    immediately, not at slice-3 ``add_job`` time."""
    with pytest.raises(ValueError, match="misfire_grace_time"):
        _make_binding(tmp_path, misfire_grace_time=bad)


def test_misfire_grace_time_flows_into_job_defaults(tmp_path):
    """The construction-time value MUST wire into APScheduler's
    ``job_defaults`` so every subsequent ``add_job`` inherits
    it. Without this the slice-3 register() calls would fall
    back to APScheduler's 1-second default and silently break
    the design section 4.0.5 misfire-recovery story."""
    b = _make_binding(tmp_path, misfire_grace_time=1800)
    # APScheduler stores defaults under ``_job_defaults``.
    assert b._scheduler._job_defaults.get("misfire_grace_time") == 1800


def test_misfire_grace_time_default_is_3600(tmp_path):
    """Default flows in as 3600 per design section 4.0.5."""
    b = _make_binding(tmp_path)  # default misfire_grace_time
    assert b._scheduler._job_defaults.get("misfire_grace_time") == 3600


def test_misfire_grace_time_none_flows_into_job_defaults(tmp_path):
    b = _make_binding(tmp_path, misfire_grace_time=None)
    assert b._scheduler._job_defaults.get("misfire_grace_time") is None


@pytest.mark.parametrize(
    "field",
    ["wakeup_callable", "conn_factory", "clock", "run_id_factory", "event_id_factory"],
)
def test_non_callable_injectable_rejected(tmp_path, field):
    kwargs = dict(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: sqlite3.connect(":memory:"),
        clock=lambda: _NOW,
        run_id_factory=lambda: "r",
        event_id_factory=lambda: "e",
        jobstore_url=_jobstore_url(tmp_path),
    )
    kwargs[field] = "not-callable"
    with pytest.raises(TypeError, match=field):
        SchedulerBinding(**kwargs)


# ===========================================================================
# Lifecycle -- start / stop
# ===========================================================================


def test_is_paused_returns_false_before_start(tmp_path):
    b = _make_binding(tmp_path)
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_start_then_is_not_paused(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        assert b.is_paused() is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_start_paused_then_is_paused(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_start_twice_raises(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await b.start()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stop_before_start_is_noop(tmp_path):
    b = _make_binding(tmp_path)
    # Should not raise; should not hang.
    await b.stop()
    # State unchanged.
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_stop_after_start_clears_state(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_double_stop_is_noop(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    # Second stop is a no-op (no exception, no APScheduler
    # SchedulerNotRunningError leakage).
    await b.stop()


@pytest.mark.asyncio
async def test_stop_yields_for_deferred_shutdown(tmp_path):
    """AsyncIOScheduler.shutdown() schedules ``_shutdown``
    onto the event loop; the actual state transition lands
    on the next loop iteration. ``stop()`` must yield via
    ``asyncio.sleep(0)`` so the caller can observe the
    completed shutdown directly. Pin by re-starting
    immediately after stop() returns -- if the shutdown
    weren't complete, start() would race the deferred
    _shutdown and the scheduler state would be inconsistent.
    """
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    # Immediate re-start must succeed -- shutdown is fully
    # done by the time stop() returned.
    await b.start()
    try:
        assert b.is_paused() is False
    finally:
        await b.stop()


# ===========================================================================
# Lifecycle -- pause / resume
# ===========================================================================


@pytest.mark.asyncio
async def test_pause_before_start_raises(tmp_path):
    b = _make_binding(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        await b.pause()


@pytest.mark.asyncio
async def test_resume_before_start_raises(tmp_path):
    b = _make_binding(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        await b.resume()


@pytest.mark.asyncio
async def test_pause_after_start(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        await b.pause()
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_after_pause(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        assert b.is_paused() is True
        await b.resume()
        assert b.is_paused() is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_double_pause_is_idempotent(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        await b.pause()
        await b.pause()
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_double_resume_is_idempotent(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        await b.resume()
        await b.resume()
        assert b.is_paused() is False
    finally:
        await b.stop()


# ===========================================================================
# _fire_for skeleton
# ===========================================================================


class _ConnSpy:
    """Sentinel connection that records close() invocations."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_fire_for_invokes_wakeup_with_documented_kwargs(tmp_path):
    """The binding's APScheduler callback must call
    ``wakeup_callable`` with exactly ``conn``, ``schedule_id``,
    ``now``, ``run_id_factory``, ``event_id_factory``. ``now``
    comes from the injected clock."""
    seen: dict = {}
    spy_conn = _ConnSpy()

    def wakeup_spy(conn, **kwargs):
        seen["conn"] = conn
        seen["kwargs"] = kwargs
        return []

    fixed_run_id = lambda: "run-fixed"
    fixed_evt_id = lambda: "evt-fixed"
    b = SchedulerBinding(
        wakeup_callable=wakeup_spy,
        conn_factory=lambda: spy_conn,
        clock=lambda: _NOW,
        run_id_factory=fixed_run_id,
        event_id_factory=fixed_evt_id,
        jobstore_url=_jobstore_url(tmp_path),
    )

    b._fire_for("my_schedule")

    assert seen["conn"] is spy_conn
    assert seen["kwargs"] == {
        "schedule_id": "my_schedule",
        "now": _NOW,
        "run_id_factory": fixed_run_id,
        "event_id_factory": fixed_evt_id,
    }
    # Connection was closed even though wakeup succeeded.
    assert spy_conn.closed is True


def test_fire_for_swallows_exception_and_logs(tmp_path, caplog):
    """A wakeup that raises Exception must NOT propagate.
    The error is logged via ``logger.exception`` and the
    method returns None. Pin: the connection is closed even
    on the exception path (finally clause)."""
    spy_conn = _ConnSpy()

    def wakeup_raises(conn, **kwargs):
        raise RuntimeError("synthetic wakeup failure")

    b = SchedulerBinding(
        wakeup_callable=wakeup_raises,
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )

    with caplog.at_level(logging.ERROR, logger=binding_mod.__name__):
        # Must NOT raise.
        result = b._fire_for("broken_schedule")

    assert result is None
    # Connection closed via finally.
    assert spy_conn.closed is True
    # logger.exception ran for the broken schedule.
    assert any(
        r.levelno == logging.ERROR and "broken_schedule" in r.getMessage()
        for r in caplog.records
    )


def test_fire_for_propagates_base_exception(tmp_path):
    """A wakeup that raises BaseException (e.g.
    CancelledError) MUST propagate out of ``_fire_for`` so
    APScheduler's shutdown path can act on the signal.
    Same contract as Worker._run_loop in phase 4."""
    spy_conn = _ConnSpy()

    def wakeup_cancels(conn, **kwargs):
        raise asyncio.CancelledError("synthetic cancel")

    b = SchedulerBinding(
        wakeup_callable=wakeup_cancels,
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )

    with pytest.raises(asyncio.CancelledError):
        b._fire_for("any_schedule")
    # Connection still closed via finally even though
    # BaseException propagated.
    assert spy_conn.closed is True


def test_fire_for_closes_connection_after_factory_call_succeeds(tmp_path):
    """Pin the finally clause: the conn opened by
    conn_factory is closed regardless of the wakeup outcome.
    Already covered above; this is the explicit success
    path so future refactors can't accidentally leave it
    open on the happy path."""
    spy_conn = _ConnSpy()
    b = SchedulerBinding(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )
    b._fire_for("a_schedule")
    assert spy_conn.closed is True


# ===========================================================================
# register / list_registered (slice 3)
# ===========================================================================


@pytest.mark.asyncio
async def test_register_before_start_raises(tmp_path):
    b = _make_binding(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        b.register(_one_off_spec())


@pytest.mark.asyncio
async def test_list_registered_empty_after_start(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_list_registered_empty_before_start(tmp_path):
    """``list_registered`` returns ``[]`` without starting --
    pure query, no lifecycle dependency."""
    b = _make_binding(tmp_path)
    assert b.list_registered() == []


@pytest.mark.asyncio
async def test_register_one_off_adds_date_trigger_job(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _one_off_spec(schedule_id="my_oneoff")
        b.register(spec)
        assert b.list_registered() == ["my_oneoff"]
        job = b._scheduler.get_job("my_oneoff")
        assert job is not None
        assert isinstance(job.trigger, APSchedulerDateTrigger)
        # APScheduler stores the run_date in UTC tz internally.
        assert job.trigger.run_date == spec.trigger.at_iso_datetime
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_one_off_non_utc_timezone(tmp_path):
    """APScheduler's DateTrigger normalises ``run_date`` to
    UTC internally. The trigger fires at the same UTC instant
    regardless of the timezone arg. Pin via instant equality
    on the UTC time line."""
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        at = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
        spec = _one_off_spec(at=at, tz="Europe/Kyiv")
        b.register(spec)
        job = b._scheduler.get_job(spec.id)
        assert job is not None
        assert isinstance(job.trigger, APSchedulerDateTrigger)
        # Both ``at`` and trigger.run_date describe the same
        # UTC instant; APScheduler may have normalised the
        # tzinfo but the moment in time is preserved.
        assert job.trigger.run_date == at
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_cron_adds_cron_trigger_job(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _cron_spec(schedule_id="my_cron", cron="0 18 * * *")
        b.register(spec)
        assert b.list_registered() == ["my_cron"]
        job = b._scheduler.get_job("my_cron")
        assert job is not None
        assert isinstance(job.trigger, APSchedulerCronTrigger)
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_cron_non_utc_timezone(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _cron_spec(cron="0 18 * * *", tz="Europe/Kyiv")
        b.register(spec)
        job = b._scheduler.get_job(spec.id)
        assert job is not None
        assert str(job.trigger.timezone) == "Europe/Kyiv"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_inherits_misfire_grace_time_from_job_defaults(tmp_path):
    """Slice-2 fix: ``misfire_grace_time`` flows from binding
    construction into ``job_defaults``. The slice-3 register
    must NOT override it -- jobs inherit the design-required
    grace window."""
    b = _make_binding(tmp_path, misfire_grace_time=1800)
    await b.start(paused=True)
    try:
        b.register(_one_off_spec(schedule_id="m"))
        job = b._scheduler.get_job("m")
        assert job.misfire_grace_time == 1800
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_default_misfire_grace_time_is_3600(tmp_path):
    b = _make_binding(tmp_path)  # default
    await b.start(paused=True)
    try:
        b.register(_one_off_spec(schedule_id="m"))
        job = b._scheduler.get_job("m")
        assert job.misfire_grace_time == 3600
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_uses_module_level_fire_for_with_six_args(tmp_path):
    """APScheduler calls the registered ``func`` with ``args``
    at fire time. The binding registers the MODULE-LEVEL
    ``_fire_for`` (not the bound method) -- APScheduler
    refuses to serialise schedulers, which a bound method on
    SchedulerBinding would drag in via the binding's
    ``_scheduler`` attribute. The six args carry the
    schedule_id + the injected callables so the persisted
    job is self-contained."""
    from app.v2.runtime.binding import _fire_for as fire_for_module

    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _one_off_spec(schedule_id="route_me")
        b.register(spec)
        job = b._scheduler.get_job("route_me")
        assert job.func is fire_for_module
        args = list(job.args)
        assert args[0] == spec.id
        assert args[1] is _noop_wakeup
        assert args[2] is _memory_conn_factory
        assert args[3] is _fixed_clock
        assert args[4] is _fixed_run_id
        assert args[5] is _fixed_evt_id
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_twice_same_schedule_id_is_idempotent(tmp_path):
    """``replace_existing=True`` -- registering the same id
    twice does NOT raise. The second call replaces the first
    (verified by trigger swap below)."""
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec1 = _one_off_spec(schedule_id="same_id")
        b.register(spec1)
        # Re-register with a DIFFERENT trigger but same id.
        spec2 = _spec(
            schedule_id="same_id",
            trigger=CronTrigger(cron="0 18 * * *", timezone="UTC"),
        )
        b.register(spec2)
        # Still one job, but now cron.
        assert b.list_registered() == ["same_id"]
        job = b._scheduler.get_job("same_id")
        assert isinstance(job.trigger, APSchedulerCronTrigger)
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_cron_numeric_dow_rejected(tmp_path):
    """Numeric DOW rejected at register time via
    cron_guard.reject_numeric_dow -- BEFORE APScheduler
    sees the expression. Pin: reject is ValueError naming
    'numeric day-of-week', and no job is added."""
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _cron_spec(cron="0 18 * * 1-5")
        with pytest.raises(ValueError, match="numeric day-of-week"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_cron_invalid_expression_rejected(tmp_path):
    """A cron expression APScheduler refuses to parse (e.g.
    99 in minute field) surfaces as ValueError with the
    underlying APScheduler message attached."""
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _cron_spec(cron="99 18 * * *")
        with pytest.raises(ValueError, match="invalid cron"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_one_off_unknown_timezone_rejected(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _one_off_spec(tz="Not/A_Real_Zone")
        with pytest.raises(ValueError, match="unknown timezone"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_cron_unknown_timezone_rejected(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _cron_spec(tz="Not/A_Real_Zone")
        with pytest.raises(ValueError, match="unknown timezone"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_interval_trigger_raises_not_implemented(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _spec(
            schedule_id="interval_t",
            trigger=IntervalTrigger(every_seconds=60),
        )
        with pytest.raises(NotImplementedError, match="'interval'"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_event_trigger_raises_not_implemented(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _spec(
            schedule_id="event_t",
            trigger=EventTrigger(event="custom_signal"),
        )
        with pytest.raises(NotImplementedError, match="'event'"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_register_conditional_trigger_raises_not_implemented(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        spec = _spec(
            schedule_id="cond_t",
            trigger=ConditionalTrigger(gate="some_gate", poll_seconds=30),
        )
        with pytest.raises(NotImplementedError, match="'conditional'"):
            b.register(spec)
        assert b.list_registered() == []
    finally:
        await b.stop()


# ===========================================================================
# Hygiene smoke
# ===========================================================================


def test_binding_module_does_not_import_uuid():
    seen = set()
    for _, member in vars(binding_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_binding_module_has_no_io_imports():
    """``apscheduler.jobstores.sqlalchemy`` pulls in
    SQLAlchemy (allowed -- it's the durable jobstore per
    design section 4.0.5). Forbid only the network /
    messaging libs that have no business in this module."""
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
    for _, member in vars(binding_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"binding module imports unexpected libs: "
        f"{sorted(leaked)}"
    )


def test_binding_module_exposes_no_reasoning_or_emit_callables():
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(binding_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"binding module exposes execution-suggestive "
                f"callable: {name}"
            )


def test_binding_module_source_does_not_catch_base_exception():
    """Structural pin: ``_fire_for`` must catch Exception,
    NOT BaseException. The exact match avoids the
    docstring-mention false positive (the module docstring
    can mention ``BaseException`` for explanation as long as
    no handler line uses it).
    """
    source = inspect.getsource(binding_mod)
    # The forbidden form is the handler line literal.
    assert "except BaseException" not in source, (
        "binding must catch Exception, not BaseException -- "
        "CancelledError / shutdown signals must propagate."
    )


def test_fire_for_body_catches_exception_only():
    """AST pin: the MODULE-LEVEL ``_fire_for`` contains an
    ``except Exception:`` clause and NO ``except
    BaseException:`` clause. Walks the function AST so
    docstring substring noise doesn't apply.

    The method ``SchedulerBinding._fire_for`` is a thin
    wrapper that delegates to the module-level function;
    the exception handling lives in the module function."""
    import ast
    import textwrap

    from app.v2.runtime.binding import _fire_for as fire_for_module

    source = textwrap.dedent(inspect.getsource(fire_for_module))
    tree = ast.parse(source)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)

    saw_exception = False
    for node in ast.walk(func):
        if not isinstance(node, ast.ExceptHandler):
            continue
        et = node.type
        # ExceptHandler.type is either None (bare except),
        # a Name, or a Tuple of Names. Compare names.
        names: list[str] = []
        if isinstance(et, ast.Name):
            names = [et.id]
        elif isinstance(et, ast.Tuple):
            names = [
                e.id for e in et.elts if isinstance(e, ast.Name)
            ]
        for n in names:
            if n == "BaseException":
                pytest.fail(
                    f"_fire_for must not catch BaseException; "
                    f"found ``except {n}``"
                )
            if n == "Exception":
                saw_exception = True
    assert saw_exception, (
        "_fire_for must catch Exception in its body -- "
        "wakeup failures must be swallowed + logged so the "
        "scheduler isn't paused by a transient error."
    )
