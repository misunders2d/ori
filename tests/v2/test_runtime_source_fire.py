"""Phase 11 slices 1-3 — worker source-driven fire path.

Per ``docs/PHASE_11_PLAN.md`` §3.1 / §4 + the
claude-reviewer slice hard-check criteria.

- **Slice 1** — plan-load + source-driven routing
  (introduced the branch; made LIVE end-to-end by slices
  3+5). SELECTIVE
  failure handling: ``get_execution_plan`` is wrapped in a
  NARROW ``except ValidationError`` only — a corrupt body
  → ``_fail_run(execution_plan_invalid_at_claim)``; a
  missing row → ``_fail_run(execution_plan_missing_at_claim)``;
  a reasoning-bearing plan →
  ``_fail_run(reasoning_unsupported_pending_step_12)`` (Q4
  — a clean run_failed, NOT a raise). A TRANSIENT infra
  fault (``ConnectionNotReady`` / ``sqlite3.OperationalError``
  / DB-locked / ``sqlite3.DatabaseError``) PROPAGATES —
  the Run stays RUNNING, no ``run_failed`` is written,
  recovery retries. NO broad catch-all (it would convert a
  transient blip into a permanent FAILED of a recurring
  series — the explicit anti-goal).
- **Slice 2** — the Q6 shared atomic core +
  ``_route_source_failure_policy`` sibling (atomicity
  pinned for both paths).
- **Slice 3 (LIVE cutover)** — ``resolve_source`` is wired
  into the fire path with the EXACT phase-10 kw-args and
  ``source_id == inp.id`` (stable snapshot key). It is
  called BARE — exactly-one-terminal-outcome / never-raise
  means NO control-flow ``try/except`` around it.
  ``FAILED`` (incl. ``require_reapprove``) →
  ``_route_source_failure_policy``; ``RESOLVED`` /
  ``DRIFT`` carry the content.
- **Slice 5 (LIVE emit)** — once resolved, the content is
  posted VERBATIM via ``emit_source_to_slack``
  (``progress_strategy="whole"``) to the ``source_post``
  EmitStep channel. ``ok`` → the running → succeeded
  transition (full tick() e2e pinned); a failed emit →
  ``_route_source_failure_policy`` (reason
  ``source_emit_failed``). ``skip_unchanged`` /
  ``changed_vs_prior`` consumption is slice 6 — NOT here.
- **Q5** — the worker is a PURE snapshot consumer: it
  NEVER calls ``write_snapshot`` / ``_newest_materialised_snapshot``
  and never imports them (bound OR original name —
  hardened here at the resolve-wire slice). The per-fire
  snapshot is written by the resolver/cache under the
  DI-injected ``repo_root`` (a tmp tree in tests — never
  the working copy).
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
    def __init__(self, response=None) -> None:
        self.calls: list[dict] = []
        self.response = (
            response
            if response is not None
            else {"ok": True, "ts": "1700000000.000100"}
        )

    async def chat_postMessage(self, *, channel: str, text: str):
        self.calls.append({"channel": channel, "text": text})
        return self.response


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


def _plan(
    *, with_reasoning: bool = False, progress_strategy=None
) -> ExecutionPlan:
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
        emit=[
            EmitStep(
                id="post",
                adapter="source_post",
                # Slice 5: the source_post channel is
                # template-declared in the EmitStep args
                # (NOT spec.delivery). The slice-6 builder
                # compiles this; tests set it directly.
                args=(
                    {"channel": "C012ABCDE"}
                    if progress_strategy is None
                    else {
                        "channel": "C012ABCDE",
                        "progress_strategy": progress_strategy,
                    }
                ),
            )
        ],
    )
    return plan.with_fresh_hash()


def _build_spec(
    *,
    execution_plan_hash: str | None,
    schedule_id: str = "sched_src",
    failure_action: FailureActionType = FailureActionType.ALERT_ADMIN,
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
        failure=FailurePolicy(on_failure_action=failure_action),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
        execution_plan_hash=execution_plan_hash,
        template=None,
        authored_at=_NOW.isoformat(),
    )
    return spec.with_fresh_hash()


def _make_worker(
    factory, repo_root=None, slack_response=None
) -> Worker:
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
        slack_client=_StubSlackClient(slack_response),
        # Live source fire writes the per-fire snapshot under
        # repo_root/data/contract_audit/...; inject the test
        # tmp tree so the working copy is never littered.
        repo_root=repo_root,
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


def _seed_pending(factory, *, spec: ScheduleSpec, plan: ExecutionPlan):
    """Insert schedule + plan + a PENDING claimable Run —
    for the full tick() end-to-end (claim → RUN_STARTED →
    resolve → emit → running → succeeded)."""
    conn = factory()
    try:
        insert_execution_plan(conn, plan)
        insert_schedule(conn, spec)
        insert_run(
            conn,
            Run(
                id="run-src",
                schedule_id=spec.id,
                execution_plan_hash=spec.execution_plan_hash,
                fire_reason=FireReason.SCHEDULED,
                due_at=_NOW,
                status=RunStatus.PENDING,
                attempt=1,
                root_run_id="run-src",
            ),
        )
        conn.commit()
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


def _events(factory, run_id: str = "run-src"):
    conn = factory()
    try:
        return list_events_for_run(conn, run_id)
    finally:
        conn.close()


class _FailingExecuteConn:
    """Wrapper that raises on the ``UPDATE runs`` statement
    (fires AFTER any in-TX event appends). Mirrors the
    OneOff atomicity harness in
    test_runtime_worker_emit_branch.py."""

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", real_conn)

    def execute(self, sql, *args, **kwargs):
        if "UPDATE runs" in sql:
            raise sqlite3.IntegrityError(
                "simulated post-events failure"
            )
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name == "_real":
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


# ---------------------------------------------------------------------------
# Slice 5 — LIVE resolve → emit (progress_strategy="whole")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_whole_resolve_then_emit_dispatch_succeeds(
    tmp_path,
):
    """Direct-dispatch contract: a well-formed source-driven
    spec RESOLVES then the slice-5 emit posts the resolved
    content VERBATIM → ``_dispatch_emit_branch`` returns
    ``"succeeded"``. Channel is the source_post EmitStep
    args channel; text is the literal source bytes decoded
    1:1 (no reformat). The per-fire snapshot is written by
    the resolver/cache under the INJECTED tmp repo_root —
    NOT the working copy (Q5; no littering)."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()  # literal source text="hello" → RESOLVED
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory, repo_root=tmp_path)

    conn = factory()
    try:
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
    finally:
        conn.close()

    assert outcome == "succeeded"
    # The resolver ran (terminal event present); no failure.
    kinds = [e.kind for e in _events(factory)]
    assert EventKind.SOURCE_RESOLVED in kinds
    assert EventKind.RUN_FAILED not in kinds
    # Emit posted VERBATIM to the EmitStep-args channel.
    assert worker._slack_client.calls == [
        {"channel": "C012ABCDE", "text": "hello"}
    ]
    # Q5: resolver/cache wrote the .bin under the tmp tree.
    bins = list(
        (tmp_path / "data" / "contract_audit").rglob("*.bin")
    )
    assert bins, "resolver did not write the per-fire .bin"


@pytest.mark.asyncio
async def test_source_whole_end_to_end_run_succeeded(tmp_path):
    """Full tick() end-to-end: claim → RUN_STARTED →
    resolve → emit → running → succeeded. The Run row ends
    SUCCEEDED and the ledger carries RUN_SUCCEEDED +
    SOURCE_RESOLVED; Slack got the verbatim payload."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    _seed_pending(factory, spec=spec, plan=plan)
    worker = _make_worker(factory, repo_root=tmp_path)

    run_id = await worker.tick()

    assert run_id == "run-src"
    assert _status(factory) == "succeeded"
    kinds = [e.kind for e in _events(factory)]
    assert EventKind.RUN_STARTED in kinds
    assert EventKind.SOURCE_RESOLVED in kinds
    assert EventKind.RUN_SUCCEEDED in kinds
    assert EventKind.RUN_FAILED not in kinds
    assert worker._slack_client.calls == [
        {"channel": "C012ABCDE", "text": "hello"}
    ]


@pytest.mark.asyncio
async def test_source_emit_failure_routes_source_failure_policy(
    tmp_path,
):
    """A failed source emit (Slack ok=False) → the worker
    routes via _route_source_failure_policy (slice-2 shared
    atomic core): RUN_FAILED reason ``source_emit_failed`` +
    ADMIN_ALERT_SENT (ALERT_ADMIN), NO EMIT_FAILED (the
    OneOff-only kind), run ends FAILED."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(
        factory,
        repo_root=tmp_path,
        slack_response={"ok": False, "error": "channel_not_found"},
    )

    conn = factory()
    try:
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
    finally:
        conn.close()

    assert outcome == "failed"
    assert _status(factory) == "failed"
    assert _failed_reason(factory) == "source_emit_failed"
    kinds = {e.kind for e in _events(factory)}
    assert EventKind.SOURCE_RESOLVED in kinds  # resolve ran
    assert EventKind.ADMIN_ALERT_SENT in kinds  # ALERT_ADMIN
    assert EventKind.EMIT_FAILED not in kinds  # OneOff-only


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
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
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
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
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
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
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

    # No import binds either symbol into the module — check
    # BOTH the bound name (alias.asname or alias.name) AND
    # the ORIGINAL imported name (alias.name) so an aliased
    # import — ``from x import write_snapshot as foo`` — is
    # ALSO caught (slice-1 deferred 🔵, hardened here at the
    # resolve-wire slice per the claude-reviewer carry).
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            for alias in node.names:
                bound = alias.asname or alias.name
                original = alias.name
                assert bound not in _SNAPSHOT_WRITERS, (
                    f"worker imports {bound!r} — Q5: the worker "
                    f"must be a pure snapshot consumer"
                )
                assert original not in _SNAPSHOT_WRITERS, (
                    f"worker imports the original name "
                    f"{original!r} (aliased as {bound!r}) — "
                    f"Q5: aliasing a snapshot writer does not "
                    f"make the worker a pure consumer"
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


# ---------------------------------------------------------------------------
# Slice 2 — _route_source_failure_policy (Q6 shared atomic core)
# ---------------------------------------------------------------------------


def _route_setup(tmp_path, *, failure_action):
    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(
        execution_plan_hash=plan.hash,
        failure_action=failure_action,
    )
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory)
    return factory, spec, run, worker


@pytest.mark.asyncio
async def test_route_source_failure_alert_admin(tmp_path):
    """ALERT_ADMIN: admin_alert_sent + run_failed (reason
    carried), the running→failed UPDATE, and NO emit_failed
    (the resolver owns the terminal SOURCE_FAILED — this
    path does not double-emit)."""
    factory, spec, run, worker = _route_setup(
        tmp_path, failure_action=FailureActionType.ALERT_ADMIN
    )
    conn = factory()
    try:
        await worker._route_source_failure_policy(
            conn=conn,
            run=run,
            spec=spec,
            reason="source_security_denied",
            error_text="path escaped the allowed root",
        )
    finally:
        conn.close()

    kinds = [e.kind for e in _events(factory)]
    assert EventKind.EMIT_FAILED not in kinds
    assert EventKind.ADMIN_ALERT_SENT in kinds
    assert EventKind.RUN_FAILED in kinds
    assert _status(factory) == "failed"
    assert _failed_reason(factory) == "source_security_denied"


@pytest.mark.asyncio
async def test_route_source_failure_abort_silent(tmp_path):
    """ABORT_SILENT: run_failed + running→failed only — NO
    admin_alert_sent, NO emit_failed."""
    factory, spec, run, worker = _route_setup(
        tmp_path, failure_action=FailureActionType.ABORT_SILENT
    )
    conn = factory()
    try:
        await worker._route_source_failure_policy(
            conn=conn,
            run=run,
            spec=spec,
            reason="source_fetch_failed",
            error_text="upstream 503",
        )
    finally:
        conn.close()

    kinds = [e.kind for e in _events(factory)]
    assert EventKind.ADMIN_ALERT_SENT not in kinds
    assert EventKind.EMIT_FAILED not in kinds
    assert EventKind.RUN_FAILED in kinds
    assert _status(factory) == "failed"


@pytest.mark.asyncio
async def test_route_source_failure_retry_later_downgrades(tmp_path):
    """retry_later → downgraded to alert_admin: an
    admin_alert_sent with a ``downgrade_from`` payload key
    (the retry chain is step 14). Same shared admin builder
    as the OneOff path."""
    factory, spec, run, worker = _route_setup(
        tmp_path, failure_action=FailureActionType.RETRY_LATER
    )
    conn = factory()
    try:
        await worker._route_source_failure_policy(
            conn=conn,
            run=run,
            spec=spec,
            reason="source_fetch_failed",
            error_text="upstream timeout",
        )
    finally:
        conn.close()

    admin = [
        e
        for e in _events(factory)
        if e.kind is EventKind.ADMIN_ALERT_SENT
    ]
    assert len(admin) == 1
    assert admin[0].payload.get("downgrade_from") == "retry_later"
    assert _status(factory) == "failed"


@pytest.mark.asyncio
async def test_route_source_failure_atomic_rollback(tmp_path):
    """Shared atomic core (Q6): if the running→failed
    UPDATE raises mid-TX, the admin_alert_sent appended
    in-flight is ROLLED BACK and the Run stays RUNNING —
    same single-transaction invariant proven for the OneOff
    path in test_runtime_worker_emit_branch.py."""
    factory, spec, run, worker = _route_setup(
        tmp_path, failure_action=FailureActionType.ALERT_ADMIN
    )

    real_conn = factory()
    wrapped = _FailingExecuteConn(real_conn)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="simulated post-events failure",
        ):
            await worker._route_source_failure_policy(
                conn=wrapped,
                run=run,
                spec=spec,
                reason="source_fetch_failed",
                error_text="upstream 500",
            )
    finally:
        real_conn.close()

    persisted = {e.kind for e in _events(factory)}
    assert EventKind.ADMIN_ALERT_SENT not in persisted, (
        f"admin_alert_sent leaked through rollback: "
        f"{persisted!r}"
    )
    assert EventKind.RUN_FAILED not in persisted
    # Run stays RUNNING → recovery promotes (no permanent
    # FAILED written behind a rolled-back ledger).
    assert _status(factory) == "running"


# ---------------------------------------------------------------------------
# Slice 3 — resolve FAILED routes via _route_source_failure_policy
# ---------------------------------------------------------------------------


def _bad_literal_source_ref() -> SourceRefSpec:
    """A literal source missing the required ``text`` arg →
    LiteralSource raises SourceParseError (non-fallback) →
    the resolver returns a FAILED ResolveOutcome."""
    return SourceRefSpec(
        loader="source_literal",
        args={"source_id": "src"},  # no "text"
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=300,
            stale_max_age_seconds=3600,
            fallback_policy=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )


def _plan_failing() -> ExecutionPlan:
    plan = ExecutionPlan(
        id="p_src",
        description="source-driven plan (resolve-fail test)",
        author="tester",
        inputs=[
            InputSpec(
                id="src",
                loader="source_literal",
                source_ref=_bad_literal_source_ref(),
            )
        ],
        reasoning=[],
        emit=[EmitStep(id="post", adapter="source_post", args={})],
    )
    return plan.with_fresh_hash()


@pytest.mark.asyncio
async def test_source_resolve_failed_routes_source_failure_policy(
    tmp_path,
):
    """A non-fallback resolve failure → the resolver emits
    its terminal SOURCE_FAILED, then the worker routes via
    _route_source_failure_policy: RUN_FAILED carries the
    resolver's failure code, ADMIN_ALERT_SENT is written
    (ALERT_ADMIN), there is NO EMIT_FAILED (no double-emit),
    no Slack call, and the run ends FAILED."""
    factory, _ = _conn_factory(tmp_path)
    plan = _plan_failing()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory, repo_root=tmp_path)

    conn = factory()
    try:
        outcome, _marker = await worker._dispatch_emit_branch(
            conn, run
        )
    finally:
        conn.close()

    assert outcome == "failed"
    assert _status(factory) == "failed"
    evs = _events(factory)
    kinds = {e.kind for e in evs}
    assert EventKind.SOURCE_FAILED in kinds  # resolver terminal
    assert EventKind.EMIT_FAILED not in kinds  # no double-emit
    assert EventKind.ADMIN_ALERT_SENT in kinds  # ALERT_ADMIN
    # RUN_FAILED reason == the resolver's failure code.
    src_failed = next(
        e for e in evs if e.kind is EventKind.SOURCE_FAILED
    )
    assert _failed_reason(factory) == src_failed.payload.get("code")
    assert worker._slack_client.calls == []


# ---------------------------------------------------------------------------
# Slice 3 — exact kw-args + source_id == inp.id stability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_source_called_with_exact_kwargs_and_stable_source_id(
    tmp_path, monkeypatch
):
    """resolve_source is called with the EXACT phase-10
    kw-args, the worker's DI clock / id-factory /
    conn-factory / repo_root, ``as_of_datetime=None``,
    ``audit=spec.audit``, and ``source_id == inp.id``. A
    re-fire (a fresh run on the same schedule) reuses the
    SAME ``source_id`` (stable per-(schedule,source)
    snapshot key)."""
    from app.v2.sources.resolver import (
        ResolveOutcome,
        ResolveStatus,
    )

    factory, _ = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    run = _seed(factory, spec=spec, plan=plan)
    worker = _make_worker(factory, repo_root=tmp_path)

    captured: list[dict] = []

    async def _spy(**kw):
        captured.append(kw)
        return ResolveOutcome(
            status=ResolveStatus.RESOLVED,
            event_kind=EventKind.SOURCE_RESOLVED,
            event_id="evt-spy",
            event_emitted=True,
            source_id=kw["source_id"],
        )

    monkeypatch.setattr(worker_mod, "resolve_source", _spy)

    conn = factory()
    try:
        await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    assert len(captured) == 1
    kw = captured[0]
    inp = plan.inputs[0]
    # The worker RELOADS the plan/spec from the DB
    # (get_execution_plan / get_schedule), so ref / audit
    # are VALUE-equal, not identity. The DI callables are
    # the worker's own attrs → identity holds.
    assert kw["ref"] == inp.source_ref
    assert kw["source_id"] == inp.id == "src"
    assert kw["schedule_id"] == spec.id
    assert kw["run_id"] == run.id
    assert kw["conn_factory"] is worker._conn_factory
    assert kw["clock"] is worker._clock
    assert kw["event_id_factory"] is worker._event_id_factory
    assert kw["as_of_datetime"] is None
    assert kw["audit"] == spec.audit
    assert kw["repo_root"] is worker._repo_root

    # Re-fire: a fresh run on the SAME schedule reuses the
    # same source_id (stable snapshot key, run-independent).
    run2 = run.model_copy(update={"id": "run-src-2"})
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO runs (id, schedule_id, "
            "execution_plan_hash, fire_reason, due_at, "
            "status, attempt, root_run_id) VALUES "
            "(?, ?, ?, 'scheduled', ?, 'running', 1, ?)",
            (
                "run-src-2",
                spec.id,
                spec.execution_plan_hash,
                _NOW.isoformat(),
                "run-src-2",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    conn = factory()
    try:
        await worker._dispatch_emit_branch(conn, run2)
    finally:
        conn.close()

    assert len(captured) == 2
    assert captured[1]["source_id"] == inp.id == captured[0]["source_id"]


# ---------------------------------------------------------------------------
# Slice 3 — exactly-one-outcome/never-raise: NO try/except
# wrapped around resolve_source
# ---------------------------------------------------------------------------


def test_dispatch_does_not_wrap_resolve_source_in_try():
    """The phase-10 resolver guarantees exactly-one-terminal
    -outcome and never raises into the caller. Wrapping the
    call in a control-flow try/except would break that
    contract — pin that the resolve_source Call does NOT
    appear inside any ``try`` block in worker.py."""
    import ast

    tree = ast.parse(inspect.getsource(worker_mod))

    def _calls_resolve_source(subtree) -> bool:
        for n in ast.walk(subtree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = (
                f.id
                if isinstance(f, ast.Name)
                else f.attr
                if isinstance(f, ast.Attribute)
                else None
            )
            if name == "resolve_source":
                return True
        return False

    # resolve_source IS called somewhere (pin not vacuous).
    assert _calls_resolve_source(tree)

    # …but never inside a Try / TryStar body / handler /
    # else / finally. ``ast.TryStar`` (PEP 654 ``try*`` /
    # exception groups, py3.11+) is included so a
    # ``try* ... except*`` around resolve_source is ALSO
    # caught (deferred slice-3 🔵, hardened here at slice 4).
    _try_types = (ast.Try, ast.TryStar)
    for node in ast.walk(tree):
        if not isinstance(node, _try_types):
            continue
        for region in (
            node.body,
            node.orelse,
            node.finalbody,
            *[h.body for h in node.handlers],
        ):
            for stmt in region:
                assert not _calls_resolve_source(stmt), (
                    "resolve_source is wrapped in a try/except "
                    "— breaks the exactly-one-terminal-outcome "
                    "/ never-raise contract (it must be called "
                    "bare; switch on outcome.status only)"
                )


# ---------------------------------------------------------------------------
# Slice 6 — skip_unchanged THROUGH the worker (Option B):
# §3.3 provenance table via changed_vs_prior + the typed
# RUN_SUCCEEDED discriminator (distinguishability) + Q5
# ---------------------------------------------------------------------------


def _run_succeeded_payload(factory, run_id="run-src"):
    for ev in _events(factory, run_id):
        if ev.kind is EventKind.RUN_SUCCEEDED:
            return ev.payload
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy, changed_vs_prior, expect_emit",
    [
        # skip_unchanged: EMIT unless changed_vs_prior is
        # False (the §3.3 table, expressed via the signal
        # the worker actually consumes).
        ("skip_unchanged", False, False),  # unchanged → SKIP
        ("skip_unchanged", True, True),    # changed → EMIT
        ("skip_unchanged", None, True),    # FALLBACK_DEFAULT → EMIT
        # whole ALWAYS emits regardless of the signal.
        ("whole", False, True),
        ("whole", True, True),
    ],
)
async def test_skip_unchanged_through_worker_and_distinguishability(
    tmp_path, monkeypatch, strategy, changed_vs_prior, expect_emit
):
    from app.v2.sources.resolver import (
        ResolveOutcome,
        ResolveStatus,
    )

    factory, _ = _conn_factory(tmp_path)
    plan = _plan(progress_strategy=strategy)
    spec = _build_spec(execution_plan_hash=plan.hash)
    _seed_pending(factory, spec=spec, plan=plan)
    worker = _make_worker(factory, repo_root=tmp_path)

    async def _spy(**kw):
        return ResolveOutcome(
            status=ResolveStatus.RESOLVED,
            event_kind=EventKind.SOURCE_RESOLVED,
            event_id="evt-spy",
            event_emitted=True,
            source_id=kw["source_id"],
            content_bytes=b"hello",
            content_hash="sha256:" + "a" * 64,
            changed_vs_prior=changed_vs_prior,
        )

    # Worker NEVER re-reads the snapshot table — it consumes
    # ONLY outcome.changed_vs_prior (the spy supplies it);
    # this stubs the resolver entirely so any snapshot
    # re-read would be observable as a wrong result.
    monkeypatch.setattr(worker_mod, "resolve_source", _spy)

    run_id = await worker.tick()

    assert run_id == "run-src"
    # Skip OR deliver — BOTH are SUCCESS (skip is a no-op
    # success that rides the EXISTING running→succeeded).
    assert _status(factory) == "succeeded"
    kinds = [e.kind for e in _events(factory)]
    assert EventKind.RUN_SUCCEEDED in kinds
    assert EventKind.RUN_FAILED not in kinds
    # NO new event kind for the skip (Option B): the only
    # emit-family kinds that could appear don't.
    assert EventKind.EMIT_FAILED not in kinds

    # Slack called IFF we emitted.
    assert (worker._slack_client.calls != []) is expect_emit
    if expect_emit:
        assert worker._slack_client.calls == [
            {"channel": "C012ABCDE", "text": "hello"}
        ]

    # DISTINGUISHABILITY via the TYPED discriminator on the
    # EXISTING RUN_SUCCEEDED payload (Option B): a real
    # delivery → False; a skip no-op → True.
    payload = _run_succeeded_payload(factory)
    assert payload is not None
    assert payload["skipped_unchanged"] is (not expect_emit)
    assert payload["worker_id"] == "worker-1"
