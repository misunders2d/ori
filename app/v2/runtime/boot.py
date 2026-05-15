"""Boot sequence for the v2 runtime.

Phase 5 slice 5 per ``docs/PHASE_5_PLAN.md`` section 3.3.

Wires phase-4 storage primitives + slice-2/3/4
SchedulerBinding into a single startup hook. ``boot_runtime``
runs the 10-step sequence (recovery -> paused binding ->
backfill -> reconcile -> register -> resume -> workers);
``shutdown_runtime`` reverses the relevant parts.

Section 3.3 step ordering exists for a reason -- the
paused-start race fix (round-2 reviewer): without pausing
the scheduler during the backfill + jobstore reconciliation
phase, a persisted DateTrigger left over from a previous
boot whose ``at_iso_datetime`` passed during downtime would
fire CONCURRENTLY with the step-5 backfill, both inserting
Run rows for the same OneOff schedule.

Phase 5 ships ``boot_runtime`` + ``shutdown_runtime`` as
callables. ``run_bot.py`` integration (the production
caller) lands in phase 9 per design contract section 12
post-renumber + plan section 9.2 open question 8. Phase 5
exercises the callables via the slice-7 fast / slow e2e
tests.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` section 4.0.3, 4.0.5.
- ``docs/PHASE_5_PLAN.md`` section 3.3.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Union

from app.v2.models.triggers import OneOffTrigger
from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)
from app.v2.runtime.binding import SchedulerBinding
from app.v2.runtime.recovery import (
    RecoveredRun,
    RecoveryError,
    scan_stale_runs,
)
from app.v2.runtime.wakeup import wakeup
from app.v2.runtime.worker import Worker
from app.v2.storage.schedules import list_active_schedules


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfilledOneOff:
    """A OneOff schedule whose ``at_iso_datetime`` was past
    at boot AND had no existing Run row -- fired during
    boot backfill (plan section 3.2.2 mechanic 2)."""

    schedule_id: str
    fire_at: datetime
    run_id: str


@dataclass(frozen=True)
class RegistrationError:
    """A schedule the boot refused or failed to register.

    Categories:
    - ``"missed beyond max_backfill_age"`` -- a past-due
      OneOff was older than ``max_backfill_age`` so the
      backfill skipped it.
    - ``"backfill failed: ..."`` -- the wakeup() call
      during backfill raised.
    - any other text -- the binding's ``register(spec)``
      call raised (invalid cron, unsupported trigger,
      unknown timezone, etc).

    Boot continues past per-schedule failures; the caller
    inspects ``RuntimeHandle.registration_errors`` to
    decide whether to alert.
    """

    schedule_id: str
    error_message: str


@dataclass
class RuntimeHandle:
    """Handle returned from ``boot_runtime`` -- carries the
    live binding + worker pool plus structured boot-health
    signals the caller iterates after boot.

    Phase 9 slice 7 (round-3 reviewer L311 + L320 fix): the
    handle gains an ``activate()`` async method + a private
    ``_activated`` flag so phase-9 cutover can boot the
    runtime with ``autostart=False``, bring transports
    online, and only then start workers + resume the
    binding. ``activate()`` is one-shot per handle:
    re-calling it raises :class:`RuntimeAlreadyActivatedError`
    so accidental double-activation (e.g. a default
    ``autostart=True`` boot followed by a defensive
    ``activate()`` call) flips loudly instead of a silent
    worker double-start.
    """

    binding: SchedulerBinding
    workers: list[Worker]
    recovery_result: list[Union[RecoveredRun, RecoveryError]]
    backfilled_one_offs: list[BackfilledOneOff]
    registration_errors: list[RegistrationError]
    _activated: bool = False

    async def activate(self) -> None:
        """Start every worker, then resume the binding.

        One-shot per handle: a second call (or a call against
        a handle returned from a default ``autostart=True``
        boot, which is already activated) raises
        :class:`RuntimeAlreadyActivatedError`. The worker pool
        starts BEFORE the binding resumes so the first wakeup
        the binding fires after resume always has a worker
        available to claim the resulting Run row -- avoids
        the race where a misfire_grace_time replay fires the
        instant resume() returns but no worker is yet polling.
        """
        if self._activated:
            raise RuntimeAlreadyActivatedError(
                "RuntimeHandle.activate() already called -- "
                "the handle is one-shot. Default boot_runtime("
                "autostart=True) already activates before "
                "returning; pass autostart=False to defer "
                "activation until transports are ready."
            )
        for worker in self.workers:
            await worker.start()
        await self.binding.resume()
        self._activated = True
        _logger.info("runtime.boot.activated")


class RuntimeBootError(RuntimeError):
    """Raised by ``boot_runtime`` when a strict abort
    threshold trips.

    Currently the only threshold is
    ``abort_on_recovery_errors=True`` + any
    ``RecoveryError`` items in the recovery scan result.
    Other failures (per-schedule register / backfill)
    surface via the handle and do NOT raise.
    """


class RuntimeAlreadyActivatedError(RuntimeError):
    """Raised by :meth:`RuntimeHandle.activate` when the
    handle was already activated -- either by a default
    ``boot_runtime(autostart=True)`` boot or by a previous
    explicit ``activate()`` call.

    Phase 9 slice 7 round-2 carry-forward (L795): the
    default ``autostart=True`` path marks the handle
    activated before returning so accidental ``activate()``
    after default boot raises here instead of silently
    double-starting workers + double-resuming the binding.
    """


async def boot_runtime(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    autostart: bool = True,
    worker_count: int = 1,
    claimed_timeout: timedelta = timedelta(minutes=5),
    running_timeout: timedelta = timedelta(minutes=30),
    poll_interval: timedelta = timedelta(seconds=10),
    claim_batch_size: int = 10,
    max_backfill_age: timedelta = timedelta(hours=24),
    abort_on_recovery_errors: bool = False,
    jobstore_url: str = "sqlite:///data/scheduler-v2-jobs.db",
    misfire_grace_time: Optional[int] = 3600,
    clock: Callable[[], datetime] = prod_clock,
    run_id_factory: Callable[[], str] = prod_run_id_factory,
    event_id_factory: Callable[[], str] = prod_event_id_factory,
) -> RuntimeHandle:
    """Run the v2 runtime startup sequence.

    Section 3.3 documents the 10-step ordering. ``clock``,
    ``run_id_factory``, ``event_id_factory`` are injected
    (same pattern as the phase-4 helpers) so tests can drive
    deterministic boots; production defaults to the
    ``_defaults`` module's wiring.

    Args:
        autostart: When True (default) preserves the phase-5
            contract: ``binding.resume()`` fires + every
            worker's ``start()`` is awaited BEFORE the handle
            returns; the handle is marked activated so an
            accidental subsequent ``activate()`` call raises
            :class:`RuntimeAlreadyActivatedError` instead of
            silently double-starting workers (round-2
            carry-forward L795). When False, the binding
            stays paused, workers are constructed but NOT
            started, and the returned handle has
            ``_activated=False``; the caller must drive
            activation via ``RuntimeHandle.activate()`` once
            Slack / Telegram transports are ready (phase 9
            round-3 reviewer L311 + L320 fix).

    Raises:
        RuntimeBootError: ``abort_on_recovery_errors=True``
            and any ``RecoveryError`` surfaced.
        ValueError: invalid ``worker_count`` (<1).

    Returns: ``RuntimeHandle`` carrying the live binding +
    worker list + structured boot-health signals.
    """
    if worker_count < 1:
        raise ValueError(
            f"worker_count must be >= 1; got {worker_count}."
        )

    # 1. Open boot connection.
    boot_conn = conn_factory()
    try:
        now = clock()

        # 2. Recovery scan.
        recovery_result = scan_stale_runs(
            boot_conn,
            now=now,
            claimed_timeout=claimed_timeout,
            running_timeout=running_timeout,
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
        )
        recovered_count = sum(
            1 for r in recovery_result if isinstance(r, RecoveredRun)
        )
        recovery_errors = [
            r for r in recovery_result if isinstance(r, RecoveryError)
        ]
        _logger.info(
            "runtime.boot.recovery: recovered=%d errors=%d",
            recovered_count,
            len(recovery_errors),
        )

        # 3. Recovery abort threshold.
        if abort_on_recovery_errors and recovery_errors:
            first = recovery_errors[0]
            raise RuntimeBootError(
                f"recovery surfaced {len(recovery_errors)} "
                f"errors; aborting per "
                f"abort_on_recovery_errors=True. First: "
                f"schedule_id={first.schedule_id!r} "
                f"run_id={first.run_id!r} -- {first.error_message}"
            )

        # 4. Construct + start binding PAUSED.
        binding = SchedulerBinding(
            wakeup_callable=wakeup,
            conn_factory=conn_factory,
            clock=clock,
            run_id_factory=run_id_factory,
            event_id_factory=event_id_factory,
            jobstore_url=jobstore_url,
            misfire_grace_time=misfire_grace_time,
        )
        await binding.start(paused=True)

        # Everything from here on holds live runtime
        # resources (started scheduler thread + soon also
        # started workers). Cleanup-on-error must stop them
        # before the exception propagates, otherwise the
        # caller has no handle to shut them down. Wrap in a
        # ``try / except BaseException`` so cancellation
        # also triggers cleanup; re-raise the original.
        started_workers: list[Worker] = []
        try:
            registration_errors: list[RegistrationError] = []
            backfilled_one_offs: list[BackfilledOneOff] = []
            # Per round-N reviewer: a OneOff that was
            # skipped or failed in step 5 must ALSO be
            # excluded from step 7 register() AND have its
            # persisted DateTrigger evicted in step 6.
            # Without the exclusion, register() would add a
            # past-due DateTrigger that misfire_grace_time
            # could let APScheduler still fire after
            # resume, violating the skip policy.
            skipped_one_off_ids: set[str] = set()

            # 5. OneOff boot backfill.
            active_schedules = list_active_schedules(boot_conn)
            for spec in active_schedules:
                if not isinstance(spec.trigger, OneOffTrigger):
                    continue
                fire_at = spec.trigger.at_iso_datetime
                if fire_at > now:
                    continue  # Future OneOff -- register handles it.
                if _has_run_row(boot_conn, spec.id):
                    continue  # Already fired -- mechanic 1 skips register too.
                age = now - fire_at
                if age > max_backfill_age:
                    msg = (
                        f"missed beyond max_backfill_age "
                        f"(age={age}, max={max_backfill_age})"
                    )
                    registration_errors.append(
                        RegistrationError(
                            schedule_id=spec.id, error_message=msg
                        )
                    )
                    skipped_one_off_ids.add(spec.id)
                    _logger.warning(
                        "runtime.boot.backfill: skip schedule_id=%r %s",
                        spec.id,
                        msg,
                    )
                    continue
                try:
                    inserted = wakeup(
                        boot_conn,
                        schedule_id=spec.id,
                        now=now,
                        run_id_factory=run_id_factory,
                        event_id_factory=event_id_factory,
                    )
                except Exception as exc:
                    registration_errors.append(
                        RegistrationError(
                            schedule_id=spec.id,
                            error_message=f"backfill failed: {exc}",
                        )
                    )
                    skipped_one_off_ids.add(spec.id)
                    _logger.exception(
                        "runtime.boot.backfill: failed schedule_id=%r",
                        spec.id,
                    )
                    continue
                for run_id in inserted:
                    backfilled_one_offs.append(
                        BackfilledOneOff(
                            schedule_id=spec.id,
                            fire_at=fire_at,
                            run_id=run_id,
                        )
                    )

            _logger.info(
                "runtime.boot.backfill: backfilled=%d skipped=%d",
                len(backfilled_one_offs),
                len(skipped_one_off_ids),
            )

            # 6. Reconcile jobstore: evict persisted jobs
            # for OneOffs that (a) already fired -- have a
            # Run row OR (b) were skipped / failed in step
            # 5. Without (b), a stale persisted DateTrigger
            # could still fire after resume even though the
            # skip policy forbids it.
            for spec in active_schedules:
                if not isinstance(spec.trigger, OneOffTrigger):
                    continue
                if (
                    _has_run_row(boot_conn, spec.id)
                    or spec.id in skipped_one_off_ids
                ):
                    binding.unregister(spec.id)

            # 7. Register forward-looking schedules. Skip
            # OneOffs we just skipped in step 5 so we
            # don't re-add a past-due DateTrigger. Per-spec
            # try/except so one broken schedule does not
            # abort the whole boot.
            register_count = 0
            for spec in active_schedules:
                if spec.id in skipped_one_off_ids:
                    continue
                try:
                    binding.register(spec)
                    register_count += 1
                except Exception as exc:
                    registration_errors.append(
                        RegistrationError(
                            schedule_id=spec.id,
                            error_message=str(exc),
                        )
                    )
                    _logger.error(
                        "runtime.boot.register: schedule_id=%r error=%r",
                        spec.id,
                        exc,
                    )
            _logger.info(
                "runtime.boot.register: registered=%d errors=%d",
                register_count,
                len(registration_errors),
            )

            # 8. Resume scheduler. From here APScheduler
            # fires registered jobs on cadence; misfired
            # persisted jobs within ``misfire_grace_time``
            # replay.
            #
            # Phase 9 slice 7 (round-3 reviewer L311):
            # ``autostart=False`` skips the resume so the
            # binding stays paused until the caller drives
            # ``RuntimeHandle.activate()`` AFTER Slack /
            # Telegram transports are ready. Without this,
            # overdue OneOff replays would fire against an
            # unready Slack client during boot.
            if autostart:
                await binding.resume()

            # 9. Worker pool. Append to ``started_workers``
            # after each successful ``start()`` so a later
            # worker-start failure still has the running
            # ones in the cleanup list.
            #
            # Phase 9 slice 7 (round-3 reviewer L320):
            # ``autostart=False`` constructs the worker
            # instances but does NOT call ``start()`` -- the
            # workers land on the handle in their unstarted
            # state; ``RuntimeHandle.activate()`` starts them
            # once transports are ready. Unstarted workers
            # are appended to a separate ``unstarted_workers``
            # list so the cleanup-on-error path ONLY iterates
            # ``started_workers`` (calling ``stop()`` on a
            # never-started worker is undefined for some
            # implementations).
            unstarted_workers: list[Worker] = []
            for i in range(worker_count):
                worker = Worker(
                    conn_factory=conn_factory,
                    worker_id=f"worker-{i}",
                    poll_interval=poll_interval,
                    clock=clock,
                    run_id_factory=run_id_factory,
                    event_id_factory=event_id_factory,
                    claim_batch_size=claim_batch_size,
                )
                if autostart:
                    await worker.start()
                    started_workers.append(worker)
                else:
                    unstarted_workers.append(worker)
        except BaseException:
            # Cleanup any live workers + the started
            # binding so the caller never sees orphaned
            # runtime resources. BaseException catches
            # CancelledError too -- the cleanup is what
            # the contract requires; the original signal
            # propagates after the cleanup.
            for worker in started_workers:
                try:
                    await worker.stop()
                except Exception:
                    _logger.exception(
                        "runtime.boot.cleanup: worker.stop "
                        "failed for worker_id=%r",
                        worker.worker_id,
                    )
            try:
                await binding.stop()
            except Exception:
                _logger.exception(
                    "runtime.boot.cleanup: binding.stop failed"
                )
            raise

        # 10. Return handle.
        #
        # Phase 9 slice 7: the handle's ``workers`` list
        # carries every Worker instance (started OR
        # unstarted). ``_activated`` mirrors the autostart
        # flag so a default ``autostart=True`` boot returns
        # an already-activated handle (round-2 carry-forward
        # L795: a subsequent accidental ``activate()`` call
        # raises ``RuntimeAlreadyActivatedError`` instead of
        # silently double-starting workers).
        all_workers = started_workers + unstarted_workers
        handle = RuntimeHandle(
            binding=binding,
            workers=all_workers,
            recovery_result=recovery_result,
            backfilled_one_offs=backfilled_one_offs,
            registration_errors=registration_errors,
            _activated=autostart,
        )
        # Phase 9 slice 7: structured boot-completion log so
        # ``run_bot.py`` can correlate boot + activate stages
        # in the daemon log. ``activated`` distinguishes a
        # default-autostart boot (workers running, binding
        # resumed) from a deferred-activation boot (caller
        # will run ``activate()`` once transports come online).
        _logger.info(
            "runtime.boot.complete: workers=%d backfilled=%d "
            "register_errors=%d activated=%s",
            len(all_workers),
            len(backfilled_one_offs),
            len(registration_errors),
            autostart,
        )
        return handle
    finally:
        boot_conn.close()


async def shutdown_runtime(handle: RuntimeHandle) -> None:
    """Stop the worker pool, then the binding.

    Order matters: workers must stop BEFORE the binding so
    in-flight ticks don't try to claim against a closing
    scheduler.

    Per-step failures are caught + logged; the function
    always tries to stop the binding even if a worker stop
    raised.
    """
    for worker in handle.workers:
        try:
            await worker.stop()
        except Exception:
            _logger.exception(
                "runtime.shutdown.worker: stop failed for "
                "worker_id=%r",
                worker.worker_id,
            )
    try:
        await handle.binding.stop()
    except Exception:
        _logger.exception("runtime.shutdown.binding: stop failed")


def _has_run_row(conn: sqlite3.Connection, schedule_id: str) -> bool:
    """True iff the v2 ``runs`` table contains any row for
    ``schedule_id``. Mirrors
    ``SchedulerBinding._one_off_already_fired`` but takes an
    open connection so the boot sequence can probe via its
    own boot conn instead of opening a new connection per
    spec."""
    row = conn.execute(
        "SELECT 1 FROM runs WHERE schedule_id = ? LIMIT 1",
        (schedule_id,),
    ).fetchone()
    return row is not None


__all__ = [
    "BackfilledOneOff",
    "RegistrationError",
    "RuntimeAlreadyActivatedError",
    "RuntimeBootError",
    "RuntimeHandle",
    "boot_runtime",
    "shutdown_runtime",
]
