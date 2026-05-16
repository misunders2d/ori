"""Phase 11 slice 1 — worker source-driven routing skeleton.

Per ``docs/PHASE_11_PLAN.md`` §3.1 / §4 slice 1 + the
claude-reviewer slice-1 hard-check criteria.

Slice 1 ships ONLY the plan-load + routing decision in
``Worker._dispatch_emit_branch``. There is NO resolve / NO
emit yet, so a well-formed source-driven spec is
deterministically failed with reason
``source_fire_not_yet_wired``. The slice-1 invariants
pinned here:

- SELECTIVE failure handling. ``get_execution_plan`` is
  wrapped in a NARROW ``except ValidationError`` only:
  a corrupt frozen body → ``_fail_run(
  execution_plan_invalid_at_claim)``; a missing row →
  ``_fail_run(execution_plan_missing_at_claim)``; a
  reasoning-bearing plan → ``_fail_run(
  reasoning_unsupported_pending_step_12)`` (Q4 — a clean
  run_failed, NOT a raise). A TRANSIENT infra fault
  (``ConnectionNotReady`` / ``sqlite3.OperationalError``
  / DB-locked) PROPAGATES — the Run stays RUNNING, no
  ``run_failed`` is written, recovery retries. A broad
  catch-all would convert a transient blip into a
  permanent FAILED of a recurring series and is the
  explicit anti-goal.
- Q5 — the worker is a PURE snapshot consumer: it NEVER
  calls ``write_snapshot`` AND NEVER calls
  ``_newest_materialised_snapshot`` (neither symbol is
  imported into ``app.v2.runtime.worker``).
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    FireReason,
    LiveChangePolicy,
    RunStatus,
    ScheduleStatus,
    SourceFallbackPolicy,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    LiveSourceCachePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
    ReasoningStep,
)
from app.v2.models.run import Run
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.source_ref import SourceRefSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.runtime import worker as worker_mod
from app.v2.runtime.worker import Worker
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.events import list_events_for_run
from app.v2.storage.execution_plans import insert_execution_plan
from app.v2.storage.runs import insert_run
from app.v2.storage.schedules import insert_schedule

_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Harness (mirrors test_runtime_worker_emit_branch.py)
# ---------------------------------------------------------------------------


class _StubSlackClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_postMessage(self, *, channel: str, text: str):
        self.calls.append({"channel": channel, "text": text})
        return {"ok": True, "ts": "1700000000.000100"}


def _conn_factory(tmp_path: Path):
    db = tmp_path / "scheduler.db"
    conn = sqlite3.connect(str(db))
    runner.apply_pending(conn)
    conn.close()

    def factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory, db


def _source_ref() -> SourceRefSpec:
    return SourceRefSpec(
        loader="source_literal",
        args={"source_id": "src", "text": "hello"},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=300,
            stale_max_age_seconds=3600,
            fallback_policy=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )


def _plan(*, with_reasoning: bool = False) -> ExecutionPlan:
    reasoning = []
    if with_reasoning:
        reasoning = [
            ReasoningStep(
                id="summarise",
                entry_agent="CoordinatorAgent",
                user_template="summarise {src}",
            )
        ]
    plan = ExecutionPlan(
        id="p_src",
        description="source-driven plan (phase-11 slice-1 test)",
        author="tester",
        inputs=[
            InputSpec(
                id="src",
                loader="source_literal",
                source_ref=_source_ref(),
            )
        ],
        reasoning=reasoning,
        emit=[EmitStep(id="post", adapter="source_post", args={})],
    )
    return plan.with_fresh_hash()


def _build_spec(
    *,
    execution_plan_hash: str | None,
    schedule_id: str = "sched_src",
) -> ScheduleSpec:
    spec = ScheduleSpec(
        id=schedule_id,
        description="source-driven schedule",
        owner=UserRef(
            platform="slack",
            user_id="U_OWNER",
            display_name="Sergey",
        ),
        trigger=OneOffTrigger(
            at_iso_datetime=_NOW + timedelta(hours=1),
            timezone="UTC",
        ),
        delivery=Delivery(
            target_session_id="C012ABCDE",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
        execution_plan_hash=execution_plan_hash,
        template=None,
        authored_at=_NOW.isoformat(),
    )
    return spec.with_fresh_hash()


def _make_worker(factory) -> Worker:
    counter = {"i": 0}

    def evt_factory() -> str:
        counter["i"] += 1
        return f"evt-{counter['i']:08d}-1111-1111-1111-111111111111"

    return Worker(
        conn_factory=factory,
        worker_id="worker-1",
        poll_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
        run_id_factory=lambda: "should-not-be-called",
        event_id_factory=evt_factory,
        slack_client=_StubSlackClient(),
    )


def _seed(
    factory,
    *,
    spec: ScheduleSpec,
    plan: ExecutionPlan | None = None,
    raw_plan: tuple[str, str] | None = None,
) -> Run:
    """Insert the schedule + a RUNNING Run (direct-dispatch
    pattern). ``plan`` inserts via the typed helper;
    ``raw_plan=(hash, body_json)`` inserts a row directly
    (for the corrupt-body case)."""
    conn = factory()
    try:
        if plan is not None:
            insert_execution_plan(conn, plan)
        if raw_plan is not None:
            conn.execute(
                "INSERT INTO execution_plans "
                "(hash, body_json, enforcement, authored_at, "
                "author) VALUES (?, ?, 'strict', ?, 'tester')",
                (raw_plan[0], raw_plan[1], _NOW.isoformat()),
            )
        insert_schedule(conn, spec)
        run = Run(
            id="run-src",
            schedule_id=spec.id,
            execution_plan_hash=spec.execution_plan_hash,
            fire_reason=FireReason.SCHEDULED,
            due_at=_NOW,
            status=RunStatus.PENDING,
            attempt=1,
            root_run_id="run-src",
        )
        insert_run(conn, run)
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-src",),
        )
        conn.commit()
        return run.model_copy(update={"status": RunStatus.RUNNING})
    finally:
        conn.close()


def _delete_plan(factory, hash_: str) -> None:
    """Delete an execution_plans row with FK enforcement
    OFF — simulates the plan going missing BETWEEN run
    creation and dispatch (mirrors the phase-9
    schedule-not-found-at-claim seam)."""
    conn = factory()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "DELETE FROM execution_plans WHERE hash = ?",
            (hash_,),
        )
        conn.commit()
    finally:
        conn.close()


def _status(factory, run_id: str = "run-src") -> str:
    conn = factory()
    try:
        return conn.execute(
            "SELECT status FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _failed_reason(factory, run_id: str = "run-src") -> str | None:
    conn = factory()
    try:
        for ev in list_events_for_run(conn, run_id):
            if ev.kind is EventKind.RUN_FAILED:
                return ev.payload.get("reason")
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routing: a well-formed source-driven spec is NOT yet wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_driven_spec_not_yet_wired_fail_run(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory)

    conn = factory()
    try:
        outcome = await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    assert outcome == "failed"
    assert _status(factory) == "failed"
    assert _failed_reason(factory) == "source_fire_not_yet_wired"
    # No emit attempted in slice 1.
    assert worker._slack_client.calls == []


# ---------------------------------------------------------------------------
# Selective failure handling — the permanently-unfireable set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_plan_missing_at_claim(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    # Seed with a valid plan (schedule/run FK execution_plans),
    # then delete the plan row FK-off → it is "missing at
    # claim" while the schedule still points at the hash.
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    _delete_plan(factory, plan.hash)
    worker = _make_worker(factory)

    conn = factory()
    try:
        outcome = await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    assert outcome == "failed"
    assert _failed_reason(factory) == "execution_plan_missing_at_claim"


@pytest.mark.asyncio
async def test_execution_plan_invalid_body_at_claim(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    bad_hash = "sha256:" + "a" * 64
    # A row whose body_json is NOT a valid ExecutionPlan →
    # decode_json raises pydantic ValidationError → the
    # NARROW except converts it to a typed _fail_run.
    spec = _build_spec(execution_plan_hash=bad_hash)
    run = _seed(
        factory,
        spec=spec,
        raw_plan=(bad_hash, '{"not":"an execution plan"}'),
    )
    worker = _make_worker(factory)

    conn = factory()
    try:
        outcome = await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    assert outcome == "failed"
    assert _failed_reason(factory) == "execution_plan_invalid_at_claim"


@pytest.mark.asyncio
async def test_reasoning_bearing_plan_fail_run_not_raise(tmp_path):
    """Q4: a reasoning-bearing plan is a clean run_failed,
    NOT a raise. The step-12 boundary does not crash the
    worker."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan(with_reasoning=True)
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory)

    conn = factory()
    try:
        # Must NOT raise.
        outcome = await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    assert outcome == "failed"
    assert (
        _failed_reason(factory)
        == "reasoning_unsupported_pending_step_12"
    )


# ---------------------------------------------------------------------------
# Selective failure handling — TRANSIENT faults PROPAGATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("database is locked"),
        ConnectionNotReady("connection not ready"),
        sqlite3.DatabaseError("disk I/O error"),
    ],
)
async def test_transient_db_fault_propagates_not_fail_run(
    tmp_path, monkeypatch, exc
):
    """A transient infra fault from get_execution_plan is
    NOT caught — it propagates so the Run stays RUNNING and
    recovery retries. No run_failed is written (NOT a
    permanent FAILED of a recurring series)."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory)

    def boom(_conn, _hash):
        raise exc

    # Patch the name AS BOUND IN THE WORKER MODULE.
    monkeypatch.setattr(worker_mod, "get_execution_plan", boom)

    conn = factory()
    try:
        with pytest.raises(type(exc)):
            await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    # Run stays RUNNING; NO run_failed event.
    assert _status(factory) == "running"
    assert _failed_reason(factory) is None
    assert worker._slack_client.calls == []


# ---------------------------------------------------------------------------
# Q5 — the worker is a PURE snapshot consumer
# ---------------------------------------------------------------------------


_SNAPSHOT_WRITERS = {"write_snapshot", "_newest_materialised_snapshot"}


def test_worker_module_never_calls_or_imports_snapshot_writers():
    """AST pin: the worker NEVER CALLS and NEVER IMPORTS
    ``write_snapshot`` / ``_newest_materialised_snapshot``.
    The resolver/cache OWNS the per-fire snapshot write;
    the worker is a pure consumer (design §5.3.5 / Q5).
    A future slice that wires resolve MUST keep this true —
    ``skip_unchanged`` reads ``ResolveOutcome.changed_vs_prior``,
    never the snapshot table.

    AST-based (not a string scan) so the Q5 invariant
    DOCUMENTED in the module docstring / comments does not
    self-trigger — only an actual call or import binding
    fails this pin."""
    import ast

    tree = ast.parse(inspect.getsource(worker_mod))

    # No import binds either symbol into the module.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                assert bound not in _SNAPSHOT_WRITERS, (
                    f"worker imports {bound!r} — Q5: the worker "
                    f"must be a pure snapshot consumer"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert (
                    (alias.asname or alias.name)
                    not in _SNAPSHOT_WRITERS
                )

    # No Call resolves (by simple/attribute name) to either.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = (
            f.id
            if isinstance(f, ast.Name)
            else f.attr
            if isinstance(f, ast.Attribute)
            else None
        )
        assert name not in _SNAPSHOT_WRITERS, (
            f"worker calls {name!r} — Q5 violated: the "
            f"resolver/cache owns the snapshot write"
        )

    # Belt-and-braces: not bound on the imported module.
    for sym in _SNAPSHOT_WRITERS:
        assert not hasattr(worker_mod, sym)
