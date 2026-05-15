"""APScheduler binding for the v2 runtime.

Phase 5 slice 2 per ``docs/PHASE_5_PLAN.md`` section 3.2.

Wraps one ``apscheduler.schedulers.asyncio.AsyncIOScheduler``
instance and exposes the lifecycle + (later) registration
surface the boot sequence + lifecycle hooks call.

Slice 2 ships ONLY the lifecycle methods + the
``_fire_for`` exception-swallow skeleton. The registration
methods (``register`` / ``unregister`` / ``reregister``)
land in slices 3 and 4. The boot sequence
(``boot_runtime`` / ``shutdown_runtime``) lands in slice 5.

Job store: ``SQLAlchemyJobStore`` per design contract section
4.0.5. Default path ``sqlite:///data/scheduler-v2-jobs.db``
keeps the v2 scheduler's job store separate from v1's
``data/ori-scheduler.db`` through the phase 9 cutover.
``misfire_grace_time`` defaults to 3600 s (1 h) to match the
design line at section 4.0.5 and the value v1 already uses.

Lifecycle:

- ``start(paused: bool = False)`` -- wraps APScheduler 3.x's
  sync ``AsyncIOScheduler.start(paused=...)``. The
  ``paused=True`` path is what boot_runtime uses for
  reconciliation: jobs in the SQLAlchemy job store load but
  no fires happen until ``resume()`` is awaited.
- ``stop()`` -- wraps sync ``shutdown(wait=False)`` then
  ``await asyncio.sleep(0)`` so the actual state transition
  observable via ``is_paused()`` lands before the coroutine
  returns. AsyncIOScheduler schedules the real shutdown onto
  the event loop; without the yield, callers immediately
  observing state see the pre-shutdown value.
- ``pause()`` / ``resume()`` -- wrap the sync methods.
  Idempotent against double-pause / double-resume (the
  underlying base scheduler tolerates them).
- ``is_paused()`` -- queries ``self._scheduler.state ==
  STATE_PAUSED``. Public surface so callers (boot, tests)
  never reach into ``binding._scheduler``.

APScheduler callback exception handling:

When APScheduler fires a registered job, it invokes the
MODULE-LEVEL ``_fire_for(schedule_id, wakeup_callable,
conn_factory, clock, run_id_factory, event_id_factory)``
function. The function is module-level (not a bound method)
because APScheduler's SQLAlchemy job store refuses to
serialise schedulers, and a bound method on
``SchedulerBinding`` would drag the binding's
``_scheduler`` attribute into the serialised payload
(probed: ``TypeError: Schedulers cannot be serialized``).

``SchedulerBinding.register`` passes the binding's injected
callables into ``args`` so the persisted job carries
self-contained, serialisable references. A thin
``SchedulerBinding._fire_for`` instance method wraps the
module-level function for direct-call test convenience,
but the registered ``func`` is always the module-level form.

``_fire_for`` catches ``Exception``, logs via
``logger.exception(...)``, and SWALLOWS the error so
APScheduler's internal callback machinery never sees it.
``BaseException`` (``CancelledError`` /
``KeyboardInterrupt`` / ``SystemExit``) propagates -- same
contract as ``Worker._run_loop`` in phase 4. The job stays
registered after a swallowed exception; subsequent fires
retry. Pinned by dedicated tests.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` section 4.0.3, section
  4.0.5.
- ``docs/PHASE_5_PLAN.md`` section 3.2.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.triggers.cron import (
    CronTrigger as APSchedulerCronTrigger,
)
from apscheduler.triggers.date import DateTrigger as APSchedulerDateTrigger

from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)
from app.v2.runtime.cron_guard import reject_numeric_dow


_logger = logging.getLogger(__name__)


def _fire_for(
    schedule_id: str,
    wakeup_callable: Callable[..., list[str]],
    conn_factory: Callable[[], sqlite3.Connection],
    clock: Callable[[], datetime],
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> None:
    """Module-level callback that APScheduler invokes when a
    registered job fires.

    Module-level (not a method) so the SQLAlchemy job store
    can serialise it cleanly. APScheduler refuses to
    serialise any object whose ``__self__`` references a
    scheduler -- which a bound method on ``SchedulerBinding``
    would, via the binding's ``_scheduler`` attribute.

    ``SchedulerBinding.register`` passes the binding's
    injected callables into ``args`` at register time so the
    APScheduler job carries (schedule_id, wakeup_callable,
    conn_factory, clock, run_id_factory, event_id_factory) as
    serialisable references. In production every entry is a
    module-level function (``app.v2.runtime.wakeup.wakeup``,
    ``prod_clock``, ``prod_run_id_factory``,
    ``prod_event_id_factory`` + user-provided
    conn_factory). For tests the same shape applies --
    fixture callables MUST be module-level.

    Opens a per-fire connection via ``conn_factory``, invokes
    ``wakeup_callable`` with the phase-4 kwargs, closes the
    connection in a ``finally`` clause. Catches ``Exception``
    (logged via ``logger.exception``, swallowed). Propagates
    ``BaseException`` (``CancelledError`` /
    ``KeyboardInterrupt`` / ``SystemExit``) so APScheduler's
    shutdown path receives cancel signals cleanly.
    """
    try:
        conn = conn_factory()
        try:
            wakeup_callable(
                conn,
                schedule_id=schedule_id,
                now=clock(),
                run_id_factory=run_id_factory,
                event_id_factory=event_id_factory,
            )
        finally:
            conn.close()
    except Exception:
        # NEVER catch BaseException here. CancelledError /
        # KeyboardInterrupt / SystemExit must propagate to
        # APScheduler's shutdown path so the scheduler
        # exits cleanly. Same contract as Worker._run_loop
        # in phase 4.
        _logger.exception(
            "wakeup for schedule_id=%r failed; the job "
            "stays registered and APScheduler will fire "
            "again on its next cadence.",
            schedule_id,
        )


class SchedulerBinding:
    """Lifecycle wrapper around one ``AsyncIOScheduler``.

    See module docstring for the contract. Slice 2 ships
    construction + lifecycle; slices 3 / 4 add registration.

    Construction validates the injectables and the
    APScheduler tunables up front so a bad-arg caller never
    instantiates a half-built binding.
    """

    def __init__(
        self,
        wakeup_callable: Callable[..., list[str]],
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        clock: Callable[[], datetime] = prod_clock,
        run_id_factory: Callable[[], str] = prod_run_id_factory,
        event_id_factory: Callable[[], str] = prod_event_id_factory,
        jobstore_url: str = "sqlite:///data/scheduler-v2-jobs.db",
        misfire_grace_time: Optional[int] = 3600,
    ) -> None:
        if not callable(wakeup_callable):
            raise TypeError("wakeup_callable must be callable")
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(run_id_factory):
            raise TypeError("run_id_factory must be callable")
        if not callable(event_id_factory):
            raise TypeError("event_id_factory must be callable")
        if not jobstore_url:
            raise ValueError(
                "jobstore_url must be a non-empty SQLAlchemy URL "
                "(e.g. 'sqlite:///data/scheduler-v2-jobs.db')"
            )
        # APScheduler 3.x's Job class rejects ``misfire_grace_time
        # = 0`` with TypeError. The accepted values are a strictly
        # positive int OR None (interpreted as "no expiry --
        # always fire even if late"). Mirror the upstream contract
        # at construction so a bad value never reaches a deferred
        # ``add_job`` call where the failure mode is harder to
        # diagnose.
        if misfire_grace_time is not None and misfire_grace_time <= 0:
            raise ValueError(
                f"misfire_grace_time must be a positive int or "
                f"None; got {misfire_grace_time!r}. APScheduler "
                "rejects zero / negative values at add_job time. "
                "None means 'no expiry' (always fire, even late)."
            )

        self._wakeup_callable = wakeup_callable
        self._conn_factory = conn_factory
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory
        self._jobstore_url = jobstore_url
        self._misfire_grace_time = misfire_grace_time

        jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
        # ``job_defaults`` flows into every ``add_job`` call so
        # registered jobs inherit the design-required grace
        # window (section 4.0.5: "up to 1h by default"). Without
        # this, APScheduler's built-in default of 1 second would
        # apply -- effectively disabling misfire recovery and
        # silently breaking the boot-time replay story.
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults={"misfire_grace_time": misfire_grace_time},
        )
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, *, paused: bool = False) -> None:
        """Start the underlying ``AsyncIOScheduler``.

        ``paused=True`` flows into APScheduler's
        ``start(paused=True)`` -- the job store is loaded but
        no fires occur until ``resume()`` is awaited. Raises
        ``RuntimeError`` if already started.
        """
        if self._started:
            raise RuntimeError(
                "scheduler already started -- await stop() "
                "before starting again."
            )
        self._scheduler.start(paused=paused)
        self._started = True

    async def stop(self) -> None:
        """Shut the scheduler down. Idempotent: a no-op if
        ``start()`` was never called.

        Internally calls APScheduler's sync
        ``shutdown(wait=False)`` then yields once via
        ``asyncio.sleep(0)`` so the event-loop completes the
        deferred ``_shutdown`` task. Without the yield,
        callers that immediately inspect ``is_paused()`` or
        re-call ``start()`` would race the pending shutdown.
        """
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        # Let AsyncIOScheduler's deferred _shutdown task run.
        await asyncio.sleep(0)

    async def pause(self) -> None:
        """Pause the scheduler. No new fires until
        ``resume()`` is awaited. Idempotent against
        double-pause. Raises ``RuntimeError`` if the
        scheduler is not running."""
        if not self._started:
            raise RuntimeError(
                "scheduler not running -- start() it before "
                "calling pause()."
            )
        self._scheduler.pause()

    async def resume(self) -> None:
        """Resume the scheduler. Idempotent against
        double-resume. Raises ``RuntimeError`` if the
        scheduler is not running."""
        if not self._started:
            raise RuntimeError(
                "scheduler not running -- start() it before "
                "calling resume()."
            )
        self._scheduler.resume()

    def is_paused(self) -> bool:
        """Return True iff the scheduler is started AND in
        the PAUSED state. Returns False before ``start()``
        and after ``stop()`` -- callers can use ``is_paused``
        as a pure query without first checking lifecycle
        state.
        """
        if not self._started:
            return False
        return self._scheduler.state == STATE_PAUSED

    # ------------------------------------------------------------------
    # Registration (slice 3)
    # ------------------------------------------------------------------

    def register(self, spec: ScheduleSpec) -> None:
        """Translate ``spec.trigger`` to an APScheduler job
        and ``add_job`` it.

        - ``OneOffTrigger`` -> ``apscheduler.triggers.date
          .DateTrigger(run_date=spec.trigger.at_iso_datetime,
          timezone=ZoneInfo(spec.trigger.timezone))``.
        - ``CronTrigger`` -> ``apscheduler.triggers.cron
          .CronTrigger.from_crontab(spec.trigger.cron,
          timezone=ZoneInfo(spec.trigger.timezone))``.
          Numeric DOW is rejected by ``cron_guard
          .reject_numeric_dow`` BEFORE APScheduler sees the
          expression -- APScheduler would otherwise accept
          numeric DOW with Monday=0 semantics, silently
          diverging from Unix cron's Sunday=0.
        - ``IntervalTrigger`` / ``EventTrigger`` /
          ``ConditionalTrigger`` -> ``NotImplementedError``
          (same per-type messages as ``wakeup`` uses).

        The APScheduler job id == ``spec.id`` so callers can
        ``unregister(spec.id)`` directly. ``args=[spec.id]``
        is passed so APScheduler calls ``_fire_for(spec.id)``
        at fire time.

        ``replace_existing=True`` -- registering the same
        schedule_id twice is idempotent. The OneOff
        register-time DB guard (mechanic 1 in plan section
        3.2.2) lands in slice 4.

        ``misfire_grace_time`` flows from the binding's
        ``job_defaults`` -- no per-job override here.

        Raises:
            RuntimeError: scheduler not started.
            ValueError: invalid cron expression, numeric DOW,
                or unknown timezone.
            NotImplementedError: trigger type is interval /
                event / conditional.
        """
        if not self._started:
            raise RuntimeError(
                "scheduler not running -- call start() before "
                "register()."
            )

        trigger = spec.trigger
        if isinstance(trigger, OneOffTrigger):
            aps_trigger = self._build_one_off_aps_trigger(trigger)
        elif isinstance(trigger, CronTrigger):
            aps_trigger = self._build_cron_aps_trigger(trigger)
        elif isinstance(trigger, IntervalTrigger):
            raise NotImplementedError(
                f"Wakeup for trigger type {trigger.type!r} is "
                "not implemented in phase 5. The variant is "
                "shape-only until its use case ships in a "
                "later phase."
            )
        elif isinstance(trigger, EventTrigger):
            raise NotImplementedError(
                f"Wakeup for trigger type {trigger.type!r} is "
                "not implemented in phase 5. The variant is "
                "shape-only until its use case ships in a "
                "later phase."
            )
        elif isinstance(trigger, ConditionalTrigger):
            raise NotImplementedError(
                f"Wakeup for trigger type {trigger.type!r} is "
                "not implemented in phase 5. The variant is "
                "shape-only until its use case ships in a "
                "later phase."
            )
        else:
            # Defensive: every Trigger union member has its
            # own branch above. A new variant landing without
            # a wakeup branch surfaces here loud.
            raise NotImplementedError(
                f"Unknown trigger type "
                f"{type(trigger).__name__!r} -- binding "
                "dispatch is missing a branch. Add the "
                "variant in app/v2/runtime/binding.py."
            )

        # APScheduler registers the MODULE-LEVEL ``_fire_for``
        # (not the binding method) so the SQLAlchemy job
        # store can serialise the job without dragging the
        # binding's ``_scheduler`` attribute into the
        # serialised payload (APScheduler refuses that).
        # The injected callables flow through ``args`` so the
        # persisted job carries serialisable references to
        # them.
        self._scheduler.add_job(
            _fire_for,
            trigger=aps_trigger,
            args=[
                spec.id,
                self._wakeup_callable,
                self._conn_factory,
                self._clock,
                self._run_id_factory,
                self._event_id_factory,
            ],
            id=spec.id,
            replace_existing=True,
        )

    def list_registered(self) -> list[str]:
        """Return the ids of every job currently registered
        with the underlying scheduler. Empty when not
        started."""
        if not self._started:
            return []
        return [job.id for job in self._scheduler.get_jobs()]

    # ------------------------------------------------------------------
    # Trigger translation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_one_off_aps_trigger(
        trigger: OneOffTrigger,
    ) -> APSchedulerDateTrigger:
        tz = SchedulerBinding._resolve_zone_info(trigger.timezone)
        return APSchedulerDateTrigger(
            run_date=trigger.at_iso_datetime,
            timezone=tz,
        )

    @staticmethod
    def _build_cron_aps_trigger(
        trigger: CronTrigger,
    ) -> APSchedulerCronTrigger:
        # Reject numeric DOW BEFORE APScheduler sees the
        # expression. Reviewer round-1 finding: relying on
        # APScheduler to reject is invalid -- APScheduler
        # ACCEPTS numeric DOW with Monday=0 semantics.
        reject_numeric_dow(trigger.cron)
        tz = SchedulerBinding._resolve_zone_info(trigger.timezone)
        try:
            return APSchedulerCronTrigger.from_crontab(
                trigger.cron, timezone=tz
            )
        except ValueError as exc:
            raise ValueError(
                f"invalid cron expression {trigger.cron!r}: "
                f"{exc}"
            ) from exc

    @staticmethod
    def _resolve_zone_info(tz_name: str) -> ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"unknown timezone {tz_name!r}: {exc}."
            ) from exc

    # ------------------------------------------------------------------
    # APScheduler callback
    # ------------------------------------------------------------------

    def _fire_for(self, schedule_id: str) -> None:
        """Thin wrapper around the module-level ``_fire_for``
        function. Lets tests + direct callers invoke the fire
        path through the binding instance with the binding's
        bound callables, instead of having to thread all six
        positional args themselves.

        APScheduler registers the MODULE-LEVEL ``_fire_for``
        (not this method) -- see :meth:`register`. The
        scheduler refuses to serialise instance methods on a
        class that holds a scheduler attribute, which this
        class does.
        """
        _fire_for(
            schedule_id,
            self._wakeup_callable,
            self._conn_factory,
            self._clock,
            self._run_id_factory,
            self._event_id_factory,
        )


__all__ = ["SchedulerBinding"]
