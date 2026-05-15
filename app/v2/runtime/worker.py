"""Async worker loop for the v2 runtime.

Phase 4 slice 4 per ``docs/PHASE_4_PLAN.md`` §6.

The worker polls the ``runs`` table for due pending rows and
walks each through the empty execution path. The lifecycle is:

    pending → claimed → running → succeeded

Phase 4's execution body is intentionally empty (literally
``await asyncio.sleep(0)``) — no reasoning, no emit, no
external I/O. Real execution lands in phases 9-11.

Per-tick:
  1. ``list_claimable_due(conn, now=clock(),
     limit=claim_batch_size)``. Pre-filtered read that matches
     ``claim_run``'s predicates: ``status='pending'``,
     ``due_at <= now``, owning schedule is ``active``, and no
     other run on the schedule is currently ``claimed`` or
     ``running``. Without the pre-filter (reviewer round-4 /
     round-5 starvation), non-claimable rows at the head of
     the pending queue would block every active row behind
     them every tick.
  2. Walk the batch in due_at order, calling ``claim_run``
     on each. Refusals are now only race-loss events (another
     worker claimed the row between our read and our claim)
     — the pre-filter eliminates the structural refusal
     cases. The tick still claims AT MOST ONE row — iteration
     stops on the first successful claim.
  3. State-machine gate: ``claimed → running``. Then
     ``update_run_status_and_append_event`` writes the
     ``run_started`` event in the same TX as the UPDATE.
  4. Empty body — ``await asyncio.sleep(0)``.
  5. State-machine gate: ``running → succeeded``. Then
     ``update_run_status_and_append_event`` writes the
     ``run_succeeded`` event in the same TX.

Every timestamp comes from ``clock()``. Every new id comes
from ``event_id_factory()``. The worker does NOT import
``datetime.datetime`` for ``now()`` or ``uuid`` for ``uuid4()``
directly — smoke tests pin this.

``run_id_factory`` is accepted in the constructor for
forward-compatibility with later phases (the retry-on-failure
path will insert new pending Run rows). Phase 4's worker body
never fails, so the factory is not invoked in this slice. A
test pins that it's never called during a successful tick so a
regression that quietly starts using it surfaces here.

Connection lifetime: the worker holds ONE long-lived
connection between ``start()`` and ``stop()`` (plan §12 open
question 4: long-lived per plan default). The connection is
opened lazily via the injected ``conn_factory`` so tests can
swap in a synthetic SQLite path. ``tick()`` may be invoked
directly without ``start()`` — it opens a per-call connection
when the long-lived one is absent.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1, §4.0.4
- ``docs/PHASE_4_PLAN.md`` §6
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Optional

from app.v2.emit.slack_reminder import (
    SlackPostResult,
    SlackProtocol,
    emit_reminder_to_slack,
)
from app.v2.enums import (
    EventKind,
    FailureActionType,
    RunStatus,
    ScheduleStatus,
)
from app.v2.models.event import Event
from app.v2.models.schedule import ScheduleSpec
from app.v2.runtime.claim import claim_run
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.storage.events import append_event
from app.v2.storage.runs import list_claimable_due
from app.v2.storage.schedules import get_schedule
from app.v2.storage.transactions import update_run_status_and_append_event
from app.v2.templates.one_off_reminder import (
    ONE_OFF_REMINDER_TEMPLATE_NAME,
)


class UnsupportedSpecError(Exception):
    """Raised by the worker emit branch when a claimed Run's
    ScheduleSpec is outside the phase-9 emit-only contract.

    Examples:
    - ``execution_plan_hash`` is set (phase 10 / 12
      implements ExecutionPlan execution).
    - ``template is None`` AND ``execution_plan_hash is
      None`` (CustomFlow without plan; phase 10 / 12
      implements this path).

    The worker's run_loop catches it and logs; the Run
    stays in ``RUNNING`` and recovery on next boot
    promotes it via the phase-4 claimed-stale path.
    """


class UnsupportedFailurePolicyError(Exception):
    """Raised internally when a FailurePolicy action is not
    implemented in phase 9. Caught by
    :meth:`_route_failure_policy` and downgraded to a
    warning log + alert_admin semantics."""


_logger = logging.getLogger(__name__)


class Worker:
    """Async polling worker. See module docstring for the
    per-tick lifecycle.

    Constructor (all kwargs except ``conn_factory`` /
    ``worker_id`` are keyword-only and required — production
    wiring picks them once, tests pick deterministic ones):

        Worker(
            conn_factory: Callable[[], sqlite3.Connection],
            worker_id: str,
            *,
            poll_interval: timedelta,
            clock: Callable[[], datetime],
            run_id_factory: Callable[[], str],
            event_id_factory: Callable[[], str],
            claim_batch_size: int = 10,
        )

    ``poll_interval`` must be strictly positive — a zero or
    negative interval would busy-loop the event loop without
    yielding. ``worker_id`` must be non-empty — it's the value
    written into ``runs.claimed_by`` and into every emitted
    event's payload, so the audit ledger can attribute work.
    ``claim_batch_size`` (>= 1, default 10) bounds the per-tick
    read of claimable pending rows. The read itself filters out
    structurally non-claimable rows (paused/archived schedule,
    same-schedule already claimed/running) so head-of-queue
    starvation cannot occur. The batch only matters for race
    resilience — if another worker claims a candidate first,
    the tick walks the rest of the batch before sleeping.
    """

    def __init__(
        self,
        conn_factory: Callable[[], sqlite3.Connection],
        worker_id: str,
        *,
        poll_interval: timedelta,
        clock: Callable[[], datetime],
        run_id_factory: Callable[[], str],
        event_id_factory: Callable[[], str],
        claim_batch_size: int = 10,
        slack_client: Optional[SlackProtocol] = None,
    ) -> None:
        if not worker_id:
            raise ValueError(
                "worker_id must be non-empty — it's written into "
                "runs.claimed_by and every event payload's "
                "'worker_id' field."
            )
        if poll_interval <= timedelta(0):
            raise ValueError(
                f"poll_interval must be positive; got "
                f"{poll_interval!r}. Zero would busy-loop the "
                "event loop without yielding."
            )
        if claim_batch_size < 1:
            raise ValueError(
                f"claim_batch_size must be >= 1; got "
                f"{claim_batch_size}. The worker reads up to "
                "this many oldest-due pending rows per tick and "
                "tries each in order until one claim succeeds; "
                "a zero / negative value would mean 'never look "
                "at any row'."
            )
        if not callable(conn_factory):
            raise TypeError("conn_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(run_id_factory):
            raise TypeError("run_id_factory must be callable")
        if not callable(event_id_factory):
            raise TypeError("event_id_factory must be callable")

        self._conn_factory = conn_factory
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._event_id_factory = event_id_factory
        self._claim_batch_size = claim_batch_size
        # Phase 9 slice 5: when set, the worker fetches the
        # ScheduleSpec via ``get_schedule`` after RUN_STARTED
        # and dispatches OneOffReminder specs through the
        # emit branch. When None (backwards-compat with
        # phase-4 tests that do not seed schedules), the
        # body stays the empty ``asyncio.sleep(0)`` from
        # phase 4 and ticks proceed straight to
        # RUN_SUCCEEDED.
        self._slack_client = slack_client

        self._conn: Optional[sqlite3.Connection] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        """Open the long-lived connection and spawn the poll
        loop task. Raises ``RuntimeError`` if already started."""
        if self._task is not None:
            raise RuntimeError(
                f"worker {self._worker_id!r} already started — "
                "stop() before starting again."
            )
        self._stop_event = asyncio.Event()
        self._conn = self._conn_factory()
        self._task = asyncio.create_task(
            self._run_loop(), name=f"v2-worker-{self._worker_id}"
        )

    async def stop(self) -> None:
        """Signal the loop to stop, await the current tick, and
        close the long-lived connection. Idempotent — calling
        stop() on a worker that never started is a no-op."""
        if self._task is None:
            return
        assert self._stop_event is not None
        self._stop_event.set()
        try:
            await self._task
        finally:
            self._task = None
            self._stop_event = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def tick(self) -> Optional[str]:
        """Run one poll-claim-execute cycle.

        Returns the run id that walked through the lifecycle
        this tick, or ``None`` when there was no pending due
        run or the claim lost the race.

        Public so tests can drive a single cycle without
        spinning the full loop. If called outside of
        ``start()`` / ``stop()`` brackets, opens a per-call
        connection via ``conn_factory`` and closes it on
        return. Inside start/stop, reuses the long-lived
        connection.
        """
        if self._conn is not None:
            return await self._tick_with_conn(self._conn)
        conn = self._conn_factory()
        try:
            return await self._tick_with_conn(conn)
        finally:
            conn.close()

    async def _tick_with_conn(
        self, conn: sqlite3.Connection
    ) -> Optional[str]:
        now = self._clock()
        # ``list_claimable_due`` returns ONLY rows that pass
        # the same predicates ``claim_run`` enforces (active
        # schedule, no other claimed/running on the schedule)
        # — paused/archived rows and single-flight-blocked
        # rows never appear in the batch. Without this filter
        # an unbounded run of blocked head-of-queue rows would
        # starve active rows behind them every tick (reviewer
        # round-5 blocker). ``claim_run`` is still the race-
        # safe gate: another worker may claim a row between
        # this read and our claim attempt, in which case the
        # tick walks to the next candidate in the batch.
        pending = list_claimable_due(
            conn, now=now, limit=self._claim_batch_size
        )
        if not pending:
            return None

        run = None
        claim_event_id = None
        for candidate in pending:
            candidate_event_id = self._event_id_factory()
            claimed = claim_run(
                conn,
                candidate.id,
                claimed_by=self._worker_id,
                now=now,
                event_id=candidate_event_id,
            )
            if claimed:
                run = candidate
                claim_event_id = candidate_event_id
                break
            # Refused. With the claimable-due pre-filter in
            # step 1, the structural refusal cases (inactive
            # schedule, same-schedule already claimed/running)
            # don't appear here — refusal at this point means
            # another worker won a race in the gap between our
            # read and our claim. The generated event_id is
            # discarded; claim_run never wrote it.
        if run is None:
            return None

        # claimed → running. State-machine gate first so a
        # future narrowing of LEGAL_TRANSITIONS surfaces here
        # instead of silently writing the transition.
        assert_legal_transition(RunStatus.CLAIMED, RunStatus.RUNNING)
        started_at = self._clock()
        started_event = Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=started_at,
            kind=EventKind.RUN_STARTED,
            payload={"worker_id": self._worker_id},
            correlates=claim_event_id,
        )
        update_run_status_and_append_event(
            conn,
            run_id=run.id,
            new_status=RunStatus.RUNNING,
            event=started_event,
            extra_columns={"started_at": started_at},
        )

        # ---- Phase 9 slice 5: emit branch dispatch. ----
        # When ``slack_client`` is None, fall back to the
        # phase-4 empty-execution body (kept for backwards-
        # compatibility with phase-4 tests that don't seed
        # a schedule alongside the Run). When set, fetch
        # the ScheduleSpec and dispatch through the
        # OneOffReminder emit path; non-OneOff specs route
        # to ``run_failed`` reason codes per plan §3.5.
        if self._slack_client is not None:
            branch_outcome = await self._dispatch_emit_branch(
                conn, run
            )
            if branch_outcome != "succeeded":
                # ``_dispatch_emit_branch`` already wrote the
                # run_failed event + the running → failed
                # transition; return the run id so callers
                # observe a tick happened.
                return run.id
        else:
            # Empty execution body. The yield gives the event
            # loop a chance to interleave other workers /
            # shutdown signals.
            await asyncio.sleep(0)

        # running → succeeded.
        assert_legal_transition(
            RunStatus.RUNNING, RunStatus.SUCCEEDED
        )
        completed_at = self._clock()
        succeeded_event = Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=completed_at,
            kind=EventKind.RUN_SUCCEEDED,
            payload={"worker_id": self._worker_id},
            correlates=None,
        )
        update_run_status_and_append_event(
            conn,
            run_id=run.id,
            new_status=RunStatus.SUCCEEDED,
            event=succeeded_event,
            extra_columns={"completed_at": completed_at},
        )
        return run.id

    async def _dispatch_emit_branch(
        self,
        conn: sqlite3.Connection,
        run,
    ) -> str:
        """Phase 9 slice 5 emit dispatch.

        Returns ``"succeeded"`` when the emit fires
        successfully — caller proceeds with the existing
        ``running → succeeded`` transition. Returns
        ``"failed"`` when the branch already wrote a
        ``run_failed`` event + the ``running → failed``
        transition — caller returns early.

        Raises :class:`UnsupportedSpecError` when the
        ScheduleSpec is outside the phase-9 emit-only
        contract (execution_plan_hash set, or template
        is None AND no plan). The run_loop catches the
        raise and logs; the Run stays in RUNNING and
        recovery picks it up.
        """
        spec = get_schedule(conn, run.schedule_id)
        if spec is None:
            # Schedule was deleted between Run insert and the
            # emit dispatch. The events table's FK on
            # schedule_id makes writing a ``run_failed`` event
            # impossible (the missing schedule row would fail
            # the FK at INSERT). Raise instead so the
            # run_loop logs + leaves the Run in RUNNING;
            # boot recovery promotes the stale claimed/running
            # row to FAILED via the phase-4 path. The
            # ``schedule_not_found_at_claim`` reason is
            # carried verbatim on the exception so observers
            # can grep for it.
            raise UnsupportedSpecError(
                "schedule_not_found_at_claim: schedule "
                f"{run.schedule_id!r} missing at claim time; "
                "recovery on next boot promotes the Run"
            )

        if spec.status in (
            ScheduleStatus.ARCHIVED,
            ScheduleStatus.PAUSED,
        ):
            await self._fail_run(
                conn=conn,
                run=run,
                reason="schedule_inactive_at_claim",
                error_message=(
                    f"schedule {spec.id!r} status is "
                    f"{spec.status.value!r} at claim time; "
                    "defence-in-depth pin (the lifecycle "
                    "hook should have cancelled pending Runs)"
                ),
            )
            return "failed"

        # ExecutionPlan execution lands in phase 10 / 12.
        # Defence in depth: a OneOffReminder-template spec
        # should have execution_plan_hash=None per the
        # builder; if both are set, treat as out-of-scope.
        if spec.execution_plan_hash is not None:
            raise UnsupportedSpecError(
                f"schedule {spec.id!r} carries "
                f"execution_plan_hash="
                f"{spec.execution_plan_hash!r}; ExecutionPlan "
                "execution lands in phase 10 / 12"
            )

        if spec.template is None:
            raise UnsupportedSpecError(
                f"schedule {spec.id!r} has no template AND "
                "no execution_plan_hash; CustomFlow without "
                "template lands in phase 10 / 12"
            )

        template_name = spec.template.name
        if template_name != ONE_OFF_REMINDER_TEMPLATE_NAME:
            await self._fail_run(
                conn=conn,
                run=run,
                reason="schedule_template_changed_at_claim",
                error_message=(
                    f"schedule {spec.id!r} template name is "
                    f"{template_name!r}; phase 9 emits only "
                    f"{ONE_OFF_REMINDER_TEMPLATE_NAME!r}"
                ),
            )
            return "failed"

        # OneOffReminder emit branch.
        result = await emit_reminder_to_slack(
            spec=spec,
            slack_client=self._slack_client,
            clock=self._clock,
        )
        if result.ok:
            return "succeeded"

        await self._route_failure_policy(
            conn=conn,
            run=run,
            spec=spec,
            result=result,
        )
        return "failed"

    async def _fail_run(
        self,
        *,
        conn: sqlite3.Connection,
        run,
        reason: str,
        error_message: str,
    ) -> None:
        """Write a ``run_failed`` event + transition the
        Run to FAILED. Used for the schedule-fetch /
        staleness branches that short-circuit BEFORE the
        emit attempt (no emit_failed event written — no
        emit was attempted)."""
        assert_legal_transition(
            RunStatus.RUNNING, RunStatus.FAILED
        )
        completed_at = self._clock()
        failed_event = Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=completed_at,
            kind=EventKind.RUN_FAILED,
            payload={
                "worker_id": self._worker_id,
                "reason": reason,
                "error": error_message,
            },
            correlates=None,
        )
        update_run_status_and_append_event(
            conn,
            run_id=run.id,
            new_status=RunStatus.FAILED,
            event=failed_event,
            extra_columns={
                "completed_at": completed_at,
                "error": error_message,
            },
        )

    async def _route_failure_policy(
        self,
        *,
        conn: sqlite3.Connection,
        run,
        spec: ScheduleSpec,
        result: SlackPostResult,
    ) -> None:
        """Emit-failure routing per
        :class:`FailurePolicy.on_failure_action`. Phase 9
        implements ``alert_admin`` + ``abort_silent``.
        ``retry_later`` is downgraded to ``alert_admin``
        semantics with a WARNING log (phase 10 ships the
        retry chain)."""
        emit_failed_event = Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=self._clock(),
            kind=EventKind.EMIT_FAILED,
            payload={
                "worker_id": self._worker_id,
                "channel": result.channel,
                "error": result.error or "",
            },
            correlates=None,
        )
        append_event(conn, emit_failed_event)

        action = spec.failure.on_failure_action
        if action == FailureActionType.ABORT_SILENT:
            # No admin alert; just fail the Run.
            pass
        elif action == FailureActionType.ALERT_ADMIN:
            admin_event = Event(
                id=self._event_id_factory(),
                run_id=run.id,
                schedule_id=run.schedule_id,
                ts=self._clock(),
                kind=EventKind.ADMIN_ALERT_SENT,
                payload={
                    "worker_id": self._worker_id,
                    "reason": "emit_failed",
                    "error": result.error or "",
                },
                correlates=emit_failed_event.id,
            )
            append_event(conn, admin_event)
        else:
            # retry_later / custom — phase 10 / 12 work.
            # Downgrade to alert_admin semantics so phase 9
            # operators still see the failure.
            _logger.warning(
                "worker %s saw FailurePolicy.%s for "
                "schedule_id=%r; phase 9 downgrades to "
                "alert_admin (retry chain lands in phase 10)",
                self._worker_id,
                action.value,
                spec.id,
            )
            admin_event = Event(
                id=self._event_id_factory(),
                run_id=run.id,
                schedule_id=run.schedule_id,
                ts=self._clock(),
                kind=EventKind.ADMIN_ALERT_SENT,
                payload={
                    "worker_id": self._worker_id,
                    "reason": "emit_failed",
                    "error": result.error or "",
                    "downgrade_from": action.value,
                },
                correlates=emit_failed_event.id,
            )
            append_event(conn, admin_event)

        # Transition Run → FAILED.
        assert_legal_transition(
            RunStatus.RUNNING, RunStatus.FAILED
        )
        completed_at = self._clock()
        run_failed_event = Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=completed_at,
            kind=EventKind.RUN_FAILED,
            payload={
                "worker_id": self._worker_id,
                "reason": "emit_failed",
                "error": result.error or "",
            },
            correlates=emit_failed_event.id,
        )
        update_run_status_and_append_event(
            conn,
            run_id=run.id,
            new_status=RunStatus.FAILED,
            event=run_failed_event,
            extra_columns={
                "completed_at": completed_at,
                "error": result.error or "",
            },
        )

    async def _run_loop(self) -> None:
        """The poll loop. Runs until ``stop_event`` is set."""
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                try:
                    await self.tick()
                except Exception:
                    # NEVER catch BaseException here — that
                    # would swallow asyncio.CancelledError,
                    # KeyboardInterrupt, and SystemExit, which
                    # are control signals the loop must respect.
                    # Only Exception-tier errors are recoverable;
                    # log + sleep + retry on the next tick.
                    _logger.exception(
                        "worker %s tick crashed; continuing "
                        "after sleep",
                        self._worker_id,
                    )
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval.total_seconds(),
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            # Loop is over; the stop_event is set when we exit
            # cleanly. If we exit via an unexpected exception
            # the caller's stop() still completes via the
            # ``self._task = None`` path.
            pass


__all__ = [
    "UnsupportedFailurePolicyError",
    "UnsupportedSpecError",
    "Worker",
]
