"""APScheduler binding for the v2 runtime.

Phase 5 slice 2 per ``docs/PHASE_5_PLAN.md`` section 3.2.

Wraps one ``apscheduler.schedulers.asyncio.AsyncIOScheduler``
instance and exposes the lifecycle + registration surface
the boot sequence + lifecycle hooks call.

Phase-5 build-out (all shipped): slice 2 introduced the
lifecycle methods + the module-level ``_fire_for``
exception-swallow wrapper; slices 3-4 added the
registration methods (``register`` / ``unregister`` /
``reregister``); slice 5 added the boot sequence
(``boot_runtime`` / ``shutdown_runtime``, now in
``app/v2/runtime/boot.py``).

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
import pickle  # noqa: S403 -- used for picklability probe of args
import sqlite3
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.executors.pool import (
    ThreadPoolExecutor as APSchedulerThreadPoolExecutor,
)
from apscheduler.jobstores.base import JobLookupError
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
        # NOTE: picklability of conn_factory / wakeup_callable
        # / clock / id factories is validated at register()
        # / reregister() time via real pickle.dumps probe in
        # ``_check_persisted_args_picklable``. Construction
        # stays cheap so direct-invocation paths (``_fire_for``
        # called by tests without going through APScheduler)
        # do not require picklable callables.
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
        # OWNED executor: an APScheduler ThreadPoolExecutor we
        # construct + can shut down deterministically. Without
        # this, AsyncIOScheduler falls back to AsyncIOExecutor,
        # which for SYNC callables (wakeup_callable is sync)
        # dispatches via ``loop.run_in_executor(None, run_job,
        # ...)`` -- that uses asyncio's DEFAULT thread-pool
        # executor (loop._default_executor), an unbounded
        # process-wide pool of NON-DAEMON threads that no
        # single binding has the right to shut down (other
        # callers on the same loop -- ADK agents, FastAPI
        # handlers -- may have queued work there). Shutting it
        # down from binding.stop would poison the parent
        # process; leaving it alone leaks the threads at
        # interpreter exit (reviewer's slice-7b regression on
        # b739e98). The third option is what we do here:
        # supply our OWN bounded pool that no one else
        # touches, AsyncIOScheduler routes every sync wakeup
        # invocation through it, and binding.stop drains it
        # deterministically.
        #
        # ``max_workers=1`` is fine for phase 5: wakeup is an
        # O(few ms) DB insert; concurrent fires are a phase-9+
        # concern. We can lift this when production wiring
        # ships.
        executors = {
            "default": APSchedulerThreadPoolExecutor(max_workers=1)
        }
        # ``job_defaults`` flows into every ``add_job`` call so
        # registered jobs inherit the design-required grace
        # window (section 4.0.5: "up to 1h by default"). Without
        # this, APScheduler's built-in default of 1 second would
        # apply -- effectively disabling misfire recovery and
        # silently breaking the boot-time replay story.
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
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

        Three-layer cleanup contract:

        1. APScheduler-level shutdown. Calls
           ``self._scheduler.shutdown(wait=False)`` then
           yields repeatedly until
           ``self._scheduler.running`` flips to False. The
           deferred ``_shutdown`` is dispatched via
           ``@run_in_event_loop`` /
           ``call_soon_threadsafe``; a single
           ``asyncio.sleep(0)`` is not enough to guarantee it
           ran. 500 ms budget (50 * 10 ms) is well above the
           microsecond cost on a healthy loop.
        2. SQLAlchemyJobStore engine dispose (belt + braces).
           Even when APScheduler's deferred callback did
           execute the jobstore.shutdown() path, explicitly
           dispose every jobstore engine again -- dispose()
           is idempotent and guarantees the SQLAlchemy
           connection pool / sqlite file handles are released
           regardless of which code path got there first.
        3. Drain the OWNED executor thread pools. APScheduler's
           BasePoolExecutor.shutdown(wait=False) (which the
           deferred _shutdown already called) signals the
           workers to exit AFTER current work finishes but
           does NOT join them. Calling ``pool.shutdown(True)``
           a second time is idempotent and JOINS the threads
           -- guaranteeing no non-daemon thread leaks past
           binding.stop.

           Why a synchronous call and NOT
           ``asyncio.to_thread(pool.shutdown, True)``:
           ``asyncio.to_thread`` dispatches via
           ``loop.run_in_executor(None, ...)`` -- which
           lazily constructs ``loop._default_executor`` (a
           shared process-wide ThreadPoolExecutor with
           NON-DAEMON threads) on the first call. After the
           shutdown work completes, the default executor's
           worker thread idles in the pool waiting for more
           work and the process hangs at interpreter exit
           (reviewer's slice-7b regression on 185a836: we
           "owned" our APScheduler pool but reintroduced
           the leak via ``to_thread``). Calling
           ``pool.shutdown(True)`` directly briefly blocks
           the event loop while the join happens; that's
           acceptable on the shutdown path (the pool has
           ``max_workers=1`` and any in-flight wakeup is an
           O(few ms) sync DB insert).

           This is what the constructor's OWNED-executor
           choice buys us: only OUR pool is drained. The
           loop's shared default executor (which ADK agents,
           FastAPI handlers, etc. on the same loop also use)
           is never touched.
        """
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        # Layer 1: wait for APScheduler's deferred _shutdown.
        budget_exhausted = True
        for _ in range(50):
            if not self._scheduler.running:
                budget_exhausted = False
                break
            await asyncio.sleep(0.01)
        if budget_exhausted:
            _logger.error(
                "binding.stop: AsyncIOScheduler still reports "
                "running=True after 500 ms; deferred _shutdown "
                "did not execute. Falling through to belt+braces "
                "engine.dispose() + owned-pool drain."
            )
        # Layer 2: explicit engine.dispose() on every jobstore.
        # Idempotent against the APScheduler shutdown path
        # that already called it. The for-loop is defensive
        # against a future addition of secondary jobstores.
        for jobstore in list(self._scheduler._jobstores.values()):
            engine = getattr(jobstore, "engine", None)
            if engine is None:
                continue
            try:
                engine.dispose()
            except Exception:
                _logger.exception(
                    "binding.stop: jobstore engine.dispose failed"
                )
        # Layer 3: drain owned thread pools. See docstring
        # rationale (and the must-not-use-to_thread warning).
        # We touch only OUR pools (every executor in
        # ``self._scheduler._executors`` was constructed by
        # this binding's __init__); the loop's shared default
        # executor is never touched.
        for executor in list(self._scheduler._executors.values()):
            pool = getattr(executor, "_pool", None)
            if pool is None:
                continue
            try:
                pool.shutdown(wait=True)
            except Exception:
                _logger.exception(
                    "binding.stop: owned pool.shutdown(wait=True) failed"
                )

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
        ``unregister(spec.id)`` directly. The six-element
        ``args`` list (``[spec.id, wakeup_callable,
        conn_factory, clock, run_id_factory,
        event_id_factory]``) is passed so APScheduler calls
        the MODULE-LEVEL ``_fire_for`` with the same shape
        at fire time -- see the module docstring for why
        the callback is module-level and not a bound
        method.

        ``replace_existing=True`` -- registering the same
        schedule_id twice is idempotent. The OneOff
        register-time DB guard (mechanic 1 in plan section
        3.2.2) is applied here: a OneOff whose schedule_id
        already has any Run row in the v2 ``runs`` table is
        NOT re-registered, because resume would otherwise
        re-fire the wakeup and insert a duplicate Run.

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

        # Picklability of every persisted arg comes FIRST.
        # A lambda conn_factory would crash the OneOff DB
        # guard below ("SELECT 1 FROM runs ...") with an
        # unrelated error; failing here names the bad field
        # cleanly. Slice-3 register did the check just
        # before ``add_job``; slice-4 reviewer push moved it
        # to cover every arg (not just conn_factory) and
        # the round-N fix promoted it ahead of the guard.
        self._check_persisted_args_picklable(spec.id)

        trigger = spec.trigger
        if isinstance(trigger, OneOffTrigger):
            # OneOff register-time DB guard (plan section
            # 3.2.2 mechanic 1). If any Run row exists for
            # this schedule_id, the OneOff already fired
            # (or is in-flight) and we MUST NOT re-register
            # it -- doing so would cause a duplicate fire
            # on the next resume. Cron triggers fire
            # repeatedly so this guard does NOT apply to
            # them; the existing-run check is OneOff-only.
            if self._one_off_already_fired(spec.id):
                _logger.info(
                    "OneOff schedule_id=%r has an existing "
                    "Run row -- skipping register to avoid a "
                    "duplicate fire.",
                    spec.id,
                )
                return
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
        # them. Picklability was validated above; ``add_job``
        # can serialise safely from here.
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

    def unregister(self, schedule_id: str) -> None:
        """Remove the APScheduler job for ``schedule_id``.

        Idempotent: a no-op when no job with that id is
        registered. The boot sequence's jobstore-
        reconciliation step (plan section 3.3 step 6)
        relies on this idempotency to evict persisted
        DateTriggers whose schedule now has a Run row in
        the DB.

        Raises ``RuntimeError`` if the scheduler is not
        started.
        """
        if not self._started:
            raise RuntimeError(
                "scheduler not running -- call start() before "
                "unregister()."
            )
        try:
            self._scheduler.remove_job(schedule_id)
        except JobLookupError:
            # Idempotent -- not-yet-registered is fine.
            pass

    def reregister(self, spec: ScheduleSpec) -> None:
        """Atomically swap the trigger for an existing job.

        Calls APScheduler's ``reschedule_job(job_id,
        trigger=...)`` -- a single-call atomic trigger swap.
        NOT a ``remove_job`` + ``add_job`` pair, which would
        open a window where the job briefly does not exist
        and a concurrent fire (or boot replay) would race.

        Translates the spec's Trigger to the corresponding
        APScheduler trigger using the same helpers
        ``register`` uses (numeric DOW rejected pre-
        APScheduler via ``cron_guard``; unknown timezone
        rejected; invalid cron rejected).

        Applies the OneOff register-time DB guard (plan
        section 3.2.2 mechanic 1) when the NEW trigger is
        a OneOff: if the runs table already contains a row
        for this schedule_id, ``reregister`` raises
        ``ValueError`` instead of letting the swap proceed.
        Without this, a Cron-with-history -> OneOff revision
        would let APScheduler fire the OneOff on next
        resume and insert a duplicate Run -- exactly the
        non-idempotency the register-time guard exists to
        prevent. Callers (authoring tools) that want a
        OneOff with the same intent on a schedule that
        already fired should create a NEW schedule_id
        instead of reregistering.

        Raises:
            RuntimeError: scheduler not started.
            JobLookupError: ``spec.id`` is not currently
                registered.
            ValueError: invalid cron, numeric DOW, unknown
                timezone, OR new trigger is OneOff and the
                schedule has existing Run rows.
            NotImplementedError: trigger type is interval /
                event / conditional.
        """
        if not self._started:
            raise RuntimeError(
                "scheduler not running -- call start() before "
                "reregister()."
            )

        # Confirm the job exists BEFORE running any trigger
        # validation or the OneOff history guard. Without
        # this ordering, an unknown schedule_id paired with
        # a OneOff trigger + an existing Run row would raise
        # ValueError ("cannot reregister to OneOff ...")
        # instead of the documented JobLookupError -- the
        # caller would then misroute to "create a new
        # schedule_id" when the real fix is to call
        # register() instead of reregister().
        if self._scheduler.get_job(spec.id) is None:
            raise JobLookupError(spec.id)

        trigger = spec.trigger
        if isinstance(trigger, OneOffTrigger):
            aps_trigger = self._build_one_off_aps_trigger(trigger)
            if self._one_off_already_fired(spec.id):
                raise ValueError(
                    f"cannot reregister schedule_id={spec.id!r} "
                    "to a OneOff trigger -- the v2 runs table "
                    "already contains a row for this schedule. "
                    "Resuming the scheduler after this swap "
                    "would let APScheduler fire the new OneOff "
                    "and insert a duplicate Run. Create a NEW "
                    "schedule_id for the revised intent instead."
                )
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
            raise NotImplementedError(
                f"Unknown trigger type "
                f"{type(trigger).__name__!r} -- binding "
                "dispatch is missing a branch. Add the "
                "variant in app/v2/runtime/binding.py."
            )

        self._scheduler.reschedule_job(spec.id, trigger=aps_trigger)

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

    def _one_off_already_fired(self, schedule_id: str) -> bool:
        """Return True iff the v2 ``runs`` table has ANY row
        for ``schedule_id``.

        Plan section 3.2.2 mechanic 1: a OneOff schedule
        with an existing Run row has already fired (or is
        in-flight). Re-registering it would let APScheduler
        fire the wakeup again on the next resume, creating
        a duplicate Run row -- exactly the non-idempotency
        the phase-4 wakeup flagged.

        Opens a short-lived connection via the binding's
        ``conn_factory`` so the check uses the same DB the
        worker / wakeup / recovery share.
        """
        conn = self._conn_factory()
        try:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE schedule_id = ? LIMIT 1",
                (schedule_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _check_persisted_args_picklable(self, schedule_id: str) -> None:
        """Probe ``pickle.dumps`` on every value the binding
        passes into APScheduler's ``args=[...]`` list.

        APScheduler's SQLAlchemyJobStore pickles the args at
        ``add_job`` time. Any non-picklable value -- lambda,
        nested-function closure, class instance with a
        non-picklable attribute (e.g. a class that holds a
        lambda) -- fails deep inside APScheduler's
        serialiser with an obscure ``Can't pickle ...``
        message that does not name the offending arg.

        Probe each persisted value individually so the
        raised ``ValueError`` names the bad field. Catches
        every shape the qualname-only heuristic missed
        (most notably picklable-looking class instances
        whose internal state is not serialisable).

        Picklability is a NECESSARY condition; passing here
        means APScheduler's subsequent serialisation will
        not blow up at this level. Production wiring uses
        module-level callables (``prod_clock``,
        ``prod_run_id_factory``, ``prod_event_id_factory``,
        ``app.v2.runtime.wakeup.wakeup``) + an explicit
        ``_MigratedConnFactory``-style picklable class for
        the conn factory -- the typical test fixture shape.
        """
        # schedule_id is a str -- always picklable; included
        # in the probe loop for completeness so a future
        # refactor that adds a non-str id type surfaces here.
        args_by_name = (
            ("schedule_id", schedule_id),
            ("wakeup_callable", self._wakeup_callable),
            ("conn_factory", self._conn_factory),
            ("clock", self._clock),
            ("run_id_factory", self._run_id_factory),
            ("event_id_factory", self._event_id_factory),
        )
        for name, value in args_by_name:
            try:
                pickle.dumps(value)
            except (
                pickle.PicklingError,
                TypeError,
                AttributeError,
            ) as exc:
                raise ValueError(
                    f"{name} is not picklable -- "
                    f"APScheduler's SQLAlchemy job store "
                    f"serialises persisted job args at "
                    f"register() time and would fail with: "
                    f"{type(exc).__name__}: {exc}. Wrap the "
                    "logic in a module-level function or a "
                    "picklable class instance (e.g. a class "
                    "whose ``__init__`` stores simple state "
                    "and ``__call__`` does the work)."
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
