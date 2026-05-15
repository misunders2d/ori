"""Async worker loop for the v2 runtime.

Phase 4 slice 4 per ``docs/PHASE_4_PLAN.md`` §6.

The worker polls the ``runs`` table for due pending rows and
walks each through the empty execution path. The lifecycle is:

    pending → claimed → running → succeeded

Phase 4's execution body is intentionally empty (literally
``await asyncio.sleep(0)``) — no reasoning, no emit, no
external I/O. Real execution lands in phases 9-11.

Per-tick:
  1. ``list_pending_due(conn, now=clock(), limit=
     claim_batch_size)``. Reads up to N oldest-due pending
     rows in one go so the worker can step past pending rows
     that aren't currently claimable (inactive schedule,
     same-schedule already claimed/running). Reviewer
     round-4 blocker: a single-row read would have let the
     oldest blocked row starve every active pending row
     behind it on subsequent ticks.
  2. Walk the batch in due_at order, calling ``claim_run``
     on each. Refusals (lost race, status mismatch, inactive
     schedule, same-schedule single-flight) skip to the next
     candidate. The tick still claims AT MOST ONE row —
     iteration stops on the first successful claim.
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

from app.v2.enums import EventKind, RunStatus
from app.v2.models.event import Event
from app.v2.runtime.claim import claim_run
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.storage.runs import list_pending_due
from app.v2.storage.transactions import update_run_status_and_append_event


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
    read of pending rows: the tick scans up to this many in
    due_at order and tries each until one claim succeeds, so a
    non-claimable head-of-queue row cannot starve active rows
    behind it.
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
        # Batch read so the worker can step past pending rows
        # that aren't claimable right now (inactive schedule,
        # same-schedule already claimed/running). Without this,
        # the oldest blocked row at the head of the queue would
        # starve every active row behind it on subsequent ticks
        # — reviewer round-4 blocker. The tick still claims AT
        # MOST one row: we stop iterating as soon as one claim
        # succeeds.
        pending = list_pending_due(
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
            # Refused (lost race / single-flight / inactive
            # schedule). The id we generated for this attempt
            # is discarded — claim_run never wrote it. Try the
            # next candidate.
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

        # Empty execution body. The yield gives the event loop
        # a chance to interleave other workers / shutdown
        # signals; real reasoning + emit lands in later phases.
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


__all__ = ["Worker"]
