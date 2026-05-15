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

When APScheduler fires a registered job, it invokes a bound
method ``_fire_for(schedule_id)``. The skeleton in this
slice catches ``Exception``, logs via
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

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED

from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)


_logger = logging.getLogger(__name__)


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
        misfire_grace_time: int = 3600,
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
        if misfire_grace_time < 0:
            raise ValueError(
                f"misfire_grace_time must be >= 0; got "
                f"{misfire_grace_time}. APScheduler interprets "
                "the value as the seconds-past-due window in "
                "which a missed fire is still eligible to run; "
                "a negative number is nonsensical."
            )

        self._wakeup_callable = wakeup_callable
        self._conn_factory = conn_factory
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory
        self._jobstore_url = jobstore_url
        self._misfire_grace_time = misfire_grace_time

        jobstores = {"default": SQLAlchemyJobStore(url=jobstore_url)}
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            jobstores=jobstores,
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
    # APScheduler callback
    # ------------------------------------------------------------------

    def _fire_for(self, schedule_id: str) -> None:
        """Invoked by APScheduler when a registered job fires.

        Opens a per-fire connection via ``conn_factory``,
        invokes ``wakeup_callable`` with the injected
        clock + id factories, closes the connection, swallows
        any ``Exception`` after logging. ``BaseException``
        (``CancelledError`` / shutdown signals) propagates.

        Slice-2 skeleton: ``register`` is not yet shipped, so
        APScheduler never invokes this method in production.
        Tests call it directly to pin the contract.
        """
        try:
            conn = self._conn_factory()
            try:
                self._wakeup_callable(
                    conn,
                    schedule_id=schedule_id,
                    now=self._clock(),
                    run_id_factory=self._run_id_factory,
                    event_id_factory=self._event_id_factory,
                )
            finally:
                conn.close()
        except Exception:
            # NEVER catch BaseException here. CancelledError /
            # KeyboardInterrupt / SystemExit must propagate to
            # APScheduler's shutdown path so the scheduler
            # exits cleanly. Same contract as
            # Worker._run_loop in phase 4.
            _logger.exception(
                "wakeup for schedule_id=%r failed; the job "
                "stays registered and APScheduler will fire "
                "again on its next cadence.",
                schedule_id,
            )


__all__ = ["SchedulerBinding"]
