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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from app.v2.emit.slack_reminder import (
    SlackPostResult,
    SlackProtocol,
    emit_reminder_to_slack,
)
from app.v2.emit.source_post import emit_source_to_slack
from app.v2.enums import (
    EventKind,
    FailureActionType,
    RunStatus,
    ScheduleStatus,
)
from app.v2.models.event import Event, RunSucceededPayload
from app.v2.models.schedule import ScheduleSpec
from app.v2.runtime.claim import claim_run
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.sources.resolver import (
    ResolveOutcome,
    ResolveStatus,
    resolve_source,
)
from app.v2.storage.events import append_event
from app.v2.storage.runs import list_claimable_due
from app.v2.storage.execution_plans import get_execution_plan
from app.v2.storage.schedules import get_schedule
from app.v2.storage.transactions import (
    transaction,
    update_run_status_and_append_event,
)
from app.v2.templates.one_off_reminder import (
    ONE_OFF_REMINDER_TEMPLATE_NAME,
)


# Repo root = the dir containing app/ (this file is
# app/v2/runtime/worker.py). Default per-fire snapshot
# audit root; identical to resolver._REPO_ROOT. DI-injected
# in tests so a live source fire writes the snapshot into a
# tmp tree, never the working copy.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class UnsupportedSpecError(Exception):
    """Raised by the worker emit branch ONLY for the two
    spec shapes that cannot be cleanly failed in place
    (docs/PHASE_11_PLAN.md §3.1 / Q4):

    1. ``schedule_not_found_at_claim`` — the schedule row
       was deleted between Run insert and emit dispatch.
       The events table's FK on ``schedule_id`` makes a
       ``run_failed`` write impossible (the INSERT would
       fail the FK), so the worker cannot record a clean
       failure; it raises instead.
    2. A CustomFlow spec with neither a ``template`` NOR an
       ``execution_plan_hash`` — an unfireable shape whose
       executor lands in §12 step 12 (the reasoning /
       CustomFlow executor).

    Every OTHER out-of-contract case — a missing / invalid
    / reasoning-bearing ExecutionPlan, a source resolve or
    emit failure — is a clean ``_fail_run`` with a distinct
    reason, NOT a raise. (A source-driven spec — an
    ``execution_plan_hash`` IS set — is the LIVE §12
    step-11 fire path, resolved + emitted in
    :meth:`_dispatch_emit_branch`; it is NOT a raise.)

    The worker's run_loop catches it and logs; the Run
    stays in ``RUNNING`` and recovery on next boot
    promotes it via the phase-4 claimed-stale path.
    """


class UnsupportedFailurePolicyError(Exception):
    """Raised internally when a FailurePolicy action has no
    executor yet (the retry chain lands in §12 step 14).
    Caught by :meth:`_route_failure_policy` and downgraded
    to a warning log + alert_admin semantics."""


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
            slack_client: Optional[SlackProtocol] = None,
            repo_root: Optional[Path] = None,
        )

    ``repo_root`` (phase 11 slice 3) is the per-fire
    source-snapshot audit root threaded to
    ``resolve_source``; ``None`` → the production repo
    root. Tests inject a tmp tree so a live source fire
    never writes into the working copy.

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
        repo_root: Optional[Path] = None,
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
        # Phase 11 slice 3: per-fire source-snapshot audit
        # root threaded to ``resolve_source``. Defaults to
        # the production repo root; tests inject a tmp tree
        # so a live source fire never writes into the
        # working copy. Additive + optional — existing
        # Worker() call sites (no source path) are
        # behaviourally unchanged.
        self._repo_root = repo_root or _REPO_ROOT

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
        # Phase-11 slice 6 (Option B): a skip_unchanged
        # no-op is a SUCCESS that rides the EXISTING single
        # running → succeeded transition below — its
        # RUN_SUCCEEDED payload just carries a typed
        # discriminator. No second event, no second
        # transaction, no bypass of assert_legal_transition.
        skipped_unchanged = False
        if self._slack_client is not None:
            branch_outcome = await self._dispatch_emit_branch(
                conn, run
            )
            if branch_outcome == "failed":
                # ``_dispatch_emit_branch`` / the slice-2
                # shared atomic core already wrote the
                # run_failed event + the running → failed
                # transition; return the run id so callers
                # observe a tick happened.
                return run.id
            elif branch_outcome in (
                "succeeded",
                "succeeded_skipped",
            ):
                # EXPLICIT allowlist (slice-6 🔵): only
                # these two outcomes fall through to the ONE
                # running → succeeded write. "succeeded" is a
                # real delivery; "succeeded_skipped" is a
                # skip_unchanged no-op — only the payload
                # discriminator differs.
                skipped_unchanged = (
                    branch_outcome == "succeeded_skipped"
                )
            else:
                # Defensive else (slice-6 🔵).
                # ``_dispatch_emit_branch`` is typed to
                # return EXACTLY one of "failed" /
                # "succeeded" / "succeeded_skipped". An
                # unrecognised value must NEVER fall through
                # to the running → succeeded write below: a
                # spurious RUN_SUCCEEDED on an unhandled
                # outcome would silently mark a Run that
                # never delivered as successful. Fail the
                # Run with a distinct reason so a future
                # regression is loud in the ledger, not
                # invisible.
                await self._fail_run(
                    conn=conn,
                    run=run,
                    reason="worker_unexpected_branch_outcome",
                    error_message=(
                        f"_dispatch_emit_branch returned "
                        f"{branch_outcome!r}; expected one "
                        f"of 'failed' / 'succeeded' / "
                        f"'succeeded_skipped'. Failing the "
                        f"Run defensively rather than "
                        f"emitting a spurious RUN_SUCCEEDED."
                    ),
                )
                return run.id
        else:
            # Empty execution body. The yield gives the event
            # loop a chance to interleave other workers /
            # shutdown signals.
            await asyncio.sleep(0)

        # running → succeeded. Skip rides THIS transition —
        # the same single TX OneOff / whole-emit uses.
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
            # Typed first-class discriminator (NOT an ad-hoc
            # dict key): a real delivery leaves it the model
            # default False; a skip_unchanged no-op sets it
            # True. Additive — pre-slice-6 consumers reading
            # ``worker_id`` are unaffected.
            payload=RunSucceededPayload(
                worker_id=self._worker_id,
                skipped_unchanged=skipped_unchanged,
            ).model_dump(),
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
        """OneOff + LIVE source-driven emit dispatch
        (§12 step 11A).

        Two emit branches share this method: the phase-9
        ``OneOffReminder`` template path, and the LIVE
        source-driven fire path (§12 step 11A) — a spec
        with ``execution_plan_hash`` set is loaded,
        resolved per source input, and emitted.

        Returns ``"succeeded"`` when the emit fires
        successfully — caller proceeds with the existing
        ``running → succeeded`` transition. Returns
        ``"failed"`` when the branch already wrote a
        ``run_failed`` event + the ``running → failed``
        transition — caller returns early.

        **Raise vs _fail_run (phase-11 plan §3.1 / Q4).**
        A :class:`UnsupportedSpecError` is raised STRICTLY
        for the can't-write-a-failed-event cases — the
        events-table FK on ``schedule_id`` makes a
        ``run_failed`` write impossible when the schedule
        row is gone (``schedule_not_found_at_claim``). The
        run_loop catches it, logs, leaves the Run RUNNING,
        and boot recovery promotes it. Every other
        out-of-contract case (a missing / invalid /
        reasoning-bearing ExecutionPlan, a resolve failure)
        is a clean ``_fail_run`` with a distinct reason —
        NOT a raise.

        **Source-driven selective failure handling
        (phase-11 slice 1).** ``get_execution_plan`` is
        wrapped in a NARROW ``except ValidationError`` only
        (a corrupt / schema-incompatible frozen body →
        ``execution_plan_invalid_at_claim``). A transient
        infra fault (``ConnectionNotReady``,
        ``sqlite3.OperationalError`` / DB-locked, any
        ``sqlite3.DatabaseError``) is deliberately NOT
        caught — it propagates so ``_run_loop`` logs it,
        the Run stays RUNNING, and recovery retries.
        Converting a transient DB-locked blip into a
        permanent FAILED of a recurring series is the
        explicit anti-goal; there is NO broad ``except``.

        **Source resolve (phase-11 slice 3 — LIVE
        cutover).** Each source-bearing ``InputSpec`` is
        resolved via :func:`app.v2.sources.resolver.resolve_source`
        with ``source_id == inp.id`` (stable snapshot key
        across fires/retries) and the worker's DI
        clock / id-factory / conn-factory. The phase-10
        contract is honoured verbatim: it returns EXACTLY
        ONE terminal ``ResolveOutcome`` and NEVER raises
        into this method, so there is deliberately NO
        control-flow ``try/except`` around the call —
        wrapping it would break that contract. ``FAILED``
        (incl. ``require_reapprove`` — content withheld,
        the resolver already wrote the FRESH snapshot +
        emitted SOURCE_FAILED) routes through
        :meth:`_route_source_failure_policy`; ``RESOLVED`` /
        ``DRIFT``(alert) carry the content.

        **Source emit (phase-11 slices 5-6).** Once every
        source input is resolved, the content is posted
        VERBATIM via
        :func:`app.v2.emit.source_post.emit_source_to_slack`
        to the ``source_post`` EmitStep's channel. Slice 6
        adds ``progress_strategy`` (from the EmitStep
        args): ``"whole"`` always posts; ``"skip_unchanged"``
        reads ``outcome.changed_vs_prior`` (the slice-4
        additive field — the worker NEVER re-reads the
        snapshot table) and, when it is ``False``, returns
        ok=True + skipped_unchanged=True WITHOUT a Slack
        call. A real delivery → ``"succeeded"``; a skip
        no-op → ``"succeeded_skipped"`` (still a SUCCESS —
        the caller threads a typed discriminator onto the
        EXISTING single running → succeeded transition's
        RUN_SUCCEEDED payload; no new event / no second
        transaction — Option B). A failed emit →
        :meth:`_route_source_failure_policy` (the slice-2
        shared atomic core). The OneOff emitter is a
        SEPARATE module, byte-untouched (Q3).

        **Q5 — pure snapshot consumer (design §5.3.5).**
        The resolver/cache OWNS the per-fire snapshot
        write. This worker NEVER calls ``write_snapshot``
        AND NEVER calls ``_newest_materialised_snapshot``;
        neither symbol is imported here. ``skip_unchanged``
        (slice 6) reads ``ResolveOutcome.changed_vs_prior``
        only.
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

        # ---- §12 step 11A: LIVE source-driven fire path
        # (docs/PHASE_11_PLAN.md §3.1;
        # docs/CONTRACTS_V2_DESIGN.md §12 step 11A). A
        # source-driven spec carries execution_plan_hash →
        # an ExecutionPlan. The worker loads it, resolves
        # every source-bearing InputSpec via resolve_source
        # (FAILED / require_reapprove →
        # _route_source_failure_policy — no partial emit),
        # then posts the resolved content VERBATIM via
        # emit_source_to_slack. progress_strategy="whole"
        # always emits; "skip_unchanged" returns a no-op
        # SUCCESS when ResolveOutcome.changed_vs_prior is
        # False. Outcome: "succeeded" (delivered) /
        # "succeeded_skipped" (skip no-op) / "failed".
        if spec.execution_plan_hash is not None:
            try:
                plan = get_execution_plan(
                    conn, spec.execution_plan_hash
                )
            except ValidationError as exc:
                # NARROW catch: a frozen body that no longer
                # validates is a permanently-unfireable spec
                # (corrupt / schema-incompatible). NOT
                # transient. ConnectionNotReady /
                # sqlite3.OperationalError / DB-locked /
                # any sqlite3.DatabaseError are NOT caught —
                # they propagate (Run stays RUNNING →
                # recovery retries); a broad except that
                # _fail_run'd them would turn a transient
                # blip into a permanent FAILED of a
                # recurring series.
                await self._fail_run(
                    conn=conn,
                    run=run,
                    reason="execution_plan_invalid_at_claim",
                    error_message=(
                        f"schedule {spec.id!r} "
                        f"execution_plan_hash="
                        f"{spec.execution_plan_hash!r} body "
                        f"failed ExecutionPlan validation at "
                        f"claim time: {exc}"
                    ),
                )
                return "failed"

            if plan is None:
                await self._fail_run(
                    conn=conn,
                    run=run,
                    reason="execution_plan_missing_at_claim",
                    error_message=(
                        f"schedule {spec.id!r} references "
                        f"execution_plan_hash="
                        f"{spec.execution_plan_hash!r} with "
                        f"no execution_plans row at claim time"
                    ),
                )
                return "failed"

            if plan.reasoning:
                # Step-12 boundary (plan §1.1 / §3.1 / Q4):
                # the LLM reasoning-chain executor is not
                # built. Clean run_failed, NOT a raise.
                await self._fail_run(
                    conn=conn,
                    run=run,
                    reason="reasoning_unsupported_pending_step_12",
                    error_message=(
                        f"schedule {spec.id!r} ExecutionPlan "
                        f"carries {len(plan.reasoning)} "
                        f"reasoning step(s); the reasoning "
                        f"executor lands in §12 step 12"
                    ),
                )
                return "failed"

            # ---- Phase 11 slice 3: LIVE source resolve.
            # The phase-10 resolver is wired into the fire
            # path. ``resolve_source`` returns EXACTLY ONE
            # terminal ``ResolveOutcome`` and NEVER raises
            # into the caller — so there is deliberately NO
            # control-flow ``try/except`` around it (wrapping
            # it would break that contract). The worker
            # switches on ``outcome.status`` only.
            #
            # Q5: the resolver/cache OWNS the per-fire
            # snapshot write. The worker NEVER calls
            # ``write_snapshot`` / ``_newest_materialised_snapshot``.
            resolved: dict[str, ResolveOutcome] = {}
            for inp in plan.inputs:
                if inp.source_ref is None:
                    # Non-source loader inputs are outside
                    # the phase-11 (11A) source-driven scope.
                    continue
                outcome = await resolve_source(
                    ref=inp.source_ref,
                    source_id=inp.id,  # STABILITY: source_id == InputSpec.id
                    schedule_id=spec.id,
                    run_id=run.id,
                    conn=conn,
                    conn_factory=self._conn_factory,
                    clock=self._clock,
                    event_id_factory=self._event_id_factory,
                    as_of_datetime=None,  # live fire (not a dry-run)
                    audit=spec.audit,
                    repo_root=self._repo_root,
                    # loaders defaults to the prod SOURCE_LOADERS singleton
                )
                if outcome.status is ResolveStatus.FAILED:
                    # Every non-fallback / fetch failure AND
                    # require_reapprove land here. The resolver
                    # has ALREADY emitted its terminal
                    # SOURCE_FAILED and (for require_reapprove)
                    # written the FRESH snapshot as audit; we
                    # withhold the content and fail the run —
                    # NO partial emit.
                    # _route_source_failure_policy passes
                    # failure_event=None (no double-emit) and
                    # shares the slice-2 atomic core.
                    await self._route_source_failure_policy(
                        conn=conn,
                        run=run,
                        spec=spec,
                        reason=(
                            outcome.failure_code
                            or "source_resolve_failed"
                        ),
                        error_text=(
                            f"source {inp.id!r} resolve "
                            f"FAILED (code="
                            f"{outcome.failure_code!r}, "
                            f"fallback_eligible="
                            f"{outcome.fallback_eligible!r})"
                        ),
                    )
                    return "failed"
                # RESOLVED or DRIFT(alert_on_shape_change):
                # the resolver served content (DRIFT already
                # emitted SOURCE_DRIFT_DETECTED and STILL
                # serves). Carry it for the emit step.
                resolved[inp.id] = outcome

            # ---- Phase 11 slice 5: LIVE emit
            # (progress_strategy="whole"). Every source
            # input resolved (RESOLVED / DRIFT-alert still
            # serves); post the resolved content VERBATIM to
            # the source_post EmitStep's channel. On success
            # the caller proceeds with the existing
            # running → succeeded transition. On failure the
            # slice-2 shared atomic core
            # (_route_source_failure_policy) writes the
            # admin-alert + run_failed in ONE transaction.
            # OneOff / emit_reminder_to_slack is byte-
            # untouched (Q3 — source_post is a NEW module).
            #
            # Slice 6: under progress_strategy="skip_unchanged"
            # the adapter reads ``outcome.changed_vs_prior``
            # (slice-4 additive field — the worker NEVER
            # re-reads the snapshot table) and, if it is
            # ``False``, returns ok=True +
            # skipped_unchanged=True WITHOUT calling Slack.
            # A skip IS a success: it returns
            # "succeeded_skipped" so the caller threads the
            # discriminator onto the EXISTING single
            # running → succeeded transition's RUN_SUCCEEDED
            # payload (Option B — no new event, no second
            # transaction).
            result = await emit_source_to_slack(
                spec=spec,
                plan=plan,
                resolved=resolved,
                slack_client=self._slack_client,
                clock=self._clock,
            )
            if result.ok:
                return (
                    "succeeded_skipped"
                    if result.skipped_unchanged
                    else "succeeded"
                )
            await self._route_source_failure_policy(
                conn=conn,
                run=run,
                spec=spec,
                reason="source_emit_failed",
                error_text=result.error or "",
            )
            return "failed"

        if spec.template is None:
            raise UnsupportedSpecError(
                f"schedule {spec.id!r} has no template AND "
                "no execution_plan_hash; the CustomFlow "
                "executor lands in §12 step 12"
            )

        template_name = spec.template.name
        if template_name != ONE_OFF_REMINDER_TEMPLATE_NAME:
            await self._fail_run(
                conn=conn,
                run=run,
                reason="schedule_template_changed_at_claim",
                error_message=(
                    f"schedule {spec.id!r} template name is "
                    f"{template_name!r}; the template emit "
                    f"path serves only "
                    f"{ONE_OFF_REMINDER_TEMPLATE_NAME!r} "
                    f"(source-driven specs route via "
                    f"execution_plan_hash)"
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

    def _build_admin_alert_event(
        self,
        *,
        run,
        spec: ScheduleSpec,
        reason: str,
        error_text: str,
        correlates_id: Optional[str],
    ) -> Optional[Event]:
        """Shared admin-alert precompute (Q6, phase-11
        slice 2). ``ABORT_SILENT`` → ``None`` (NO factory /
        clock call). ``ALERT_ADMIN`` → an
        ``ADMIN_ALERT_SENT`` event. Anything else
        (``retry_later`` / custom) → downgrade to
        ``alert_admin`` semantics with a WARNING and a
        ``downgrade_from`` payload key (the retry chain is
        step 14). Pure precompute — the caller invokes it at
        the exact point in its own ``event_id_factory`` /
        ``clock`` call order where the admin event was
        historically built, so the OneOff path's event
        ids / timestamps / ordering stay BYTE-IDENTICAL."""
        action = spec.failure.on_failure_action
        if action == FailureActionType.ABORT_SILENT:
            return None
        if action == FailureActionType.ALERT_ADMIN:
            return Event(
                id=self._event_id_factory(),
                run_id=run.id,
                schedule_id=run.schedule_id,
                ts=self._clock(),
                kind=EventKind.ADMIN_ALERT_SENT,
                payload={
                    "worker_id": self._worker_id,
                    "reason": reason,
                    "error": error_text,
                },
                correlates=correlates_id,
            )
        # retry_later / custom — step-14 work. Downgrade to
        # alert_admin semantics so operators still see it.
        _logger.warning(
            "worker %s saw FailurePolicy.%s for "
            "schedule_id=%r; downgrading to alert_admin "
            "(the retry chain lands in §12 step 14)",
            self._worker_id,
            action.value,
            spec.id,
        )
        return Event(
            id=self._event_id_factory(),
            run_id=run.id,
            schedule_id=run.schedule_id,
            ts=self._clock(),
            kind=EventKind.ADMIN_ALERT_SENT,
            payload={
                "worker_id": self._worker_id,
                "reason": reason,
                "error": error_text,
                "downgrade_from": action.value,
            },
            correlates=correlates_id,
        )

    async def _commit_failure_atomic(
        self,
        *,
        conn: sqlite3.Connection,
        run,
        completed_at: datetime,
        error_text: str,
        failure_event: Optional[Event],
        admin_event: Optional[Event],
        run_failed_event: Event,
    ) -> None:
        """The round-3-hardened single-transaction commit
        core (Q6, phase-11 slice 2). Writes the optional
        failure event + the optional admin-alert event +
        the ``running → failed`` Run UPDATE + the
        ``run_failed`` event inside ONE explicit
        ``transaction(conn)`` block. Any raise inside the
        block rolls back EVERY write — the EventLedger
        never carries a failure / admin-alert event for a
        Run that still reads ``running`` (pre-fix the helper
        chained a second TX whose collision left the prior
        events committed + the Run stuck at ``running``).

        Shared by :meth:`_route_failure_policy` (OneOff —
        ``failure_event`` = the ``emit_failed`` event) and
        :meth:`_route_source_failure_policy` (source-driven
        — ``failure_event`` is ``None``: the resolver
        already emitted its terminal ``SOURCE_FAILED``; no
        double-emit). The SQL, event order, rowcount guard
        and rollback semantics are identical on both
        paths."""
        with transaction(conn):
            if failure_event is not None:
                append_event(conn, failure_event)
            if admin_event is not None:
                append_event(conn, admin_event)
            cursor = conn.execute(
                "UPDATE runs SET status = ?, completed_at = ?, "
                "error = ? WHERE id = ?",
                (
                    RunStatus.FAILED.value,
                    completed_at.astimezone(timezone.utc).isoformat(),
                    error_text,
                    run.id,
                ),
            )
            if cursor.rowcount == 0:
                # Defensive: the run row should exist by the
                # time we get here (caller saw it transition
                # to RUNNING). If it vanished, raise so the
                # transaction rolls back the events we just
                # inserted. Recovery picks up from RUNNING.
                raise RuntimeError(
                    f"_commit_failure_atomic: runs row "
                    f"{run.id!r} vanished mid-TX; rolling "
                    "back the failure + admin-alert events"
                )
            append_event(conn, run_failed_event)

    async def _route_failure_policy(
        self,
        *,
        conn: sqlite3.Connection,
        run,
        spec: ScheduleSpec,
        result: SlackPostResult,
    ) -> None:
        """OneOff emit-failure routing per
        :class:`FailurePolicy.on_failure_action`.

        **BYTE-IDENTICAL to phase 9–10 (Q6 slice-2
        constraint).** The event-id-factory / clock call
        ORDER, the three event payloads, the
        ``downgrade_from`` WARNING, the single
        ``transaction(conn)`` boundary and the rollback /
        rowcount guard are all unchanged — slice 2 only
        factors the admin precompute into
        :meth:`_build_admin_alert_event` (invoked at the
        same point the admin event was historically built)
        and the atomic commit into
        :meth:`_commit_failure_atomic`. The OneOff path
        must not regress; the existing atomicity test pins
        it.
        """
        # emit_failed precompute — UNCHANGED (factory call
        # #1, clock #1). Pre-computed OUTSIDE the TX so a
        # malformed payload raises before any write.
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

        # admin precompute (factory #2 / clock #2 when
        # emitted) — same call order as the historical
        # inline block.
        admin_event = self._build_admin_alert_event(
            run=run,
            spec=spec,
            reason="emit_failed",
            error_text=result.error or "",
            correlates_id=emit_failed_event.id,
        )

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

        await self._commit_failure_atomic(
            conn=conn,
            run=run,
            completed_at=completed_at,
            error_text=result.error or "",
            failure_event=emit_failed_event,
            admin_event=admin_event,
            run_failed_event=run_failed_event,
        )

    async def _route_source_failure_policy(
        self,
        *,
        conn: sqlite3.Connection,
        run,
        spec: ScheduleSpec,
        reason: str,
        error_text: str,
    ) -> None:
        """Source-driven resolve/emit failure routing
        (Q6, phase-11 slice 2 — sibling of
        :meth:`_route_failure_policy`).

        Wired into the fire path in slices 3 (resolve
        FAILED / ``require_reapprove``) and 5 (source emit
        failure). The resolver has ALREADY emitted its
        terminal ``SOURCE_FAILED`` event (it owns that), so
        this path does NOT emit a separate failure event —
        ``failure_event=None``. It routes the admin alert
        per :class:`FailurePolicy.on_failure_action` (via
        the SHARED :meth:`_build_admin_alert_event`) and
        commits the ``running → failed`` Run UPDATE +
        ``run_failed`` event through the SHARED
        :meth:`_commit_failure_atomic` core — same
        single-transaction atomicity invariant as the
        OneOff path. ``retry_later`` is downgraded to
        ``alert_admin`` + WARNING (retry chain = step 14)
        by the shared admin builder.
        """
        admin_event = self._build_admin_alert_event(
            run=run,
            spec=spec,
            reason=reason,
            error_text=error_text,
            correlates_id=None,
        )

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
                "reason": reason,
                "error": error_text,
            },
            correlates=None,
        )

        await self._commit_failure_atomic(
            conn=conn,
            run=run,
            completed_at=completed_at,
            error_text=error_text,
            failure_event=None,
            admin_event=admin_event,
            run_failed_event=run_failed_event,
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
