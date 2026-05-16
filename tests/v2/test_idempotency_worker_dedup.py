"""Phase 14 slice 2 — LIVE worker read-AND-write dedup.

Per ``docs/PHASE_14_PLAN.md`` §1/§9 + the claude-reviewer
fork verdict (Q-A=(a), Q-B; A.1–A.2 / B.1–B.6). The B.3
success-sub-state matrix + the B.1/B.6 atomicity pin.

Reuses the proven source-fire harness
(``test_runtime_source_fire``) so the seed is identical to
the shipped fire-path tests.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from app.v2.enums import EventKind
from app.v2.idempotency import compute_idempotency_key
from app.v2.models.event import Event
from app.v2.runtime.worker import Worker
from app.v2.storage.events import append_event
from tests.v2.test_runtime_source_fire import (
    _NOW,
    _StubSlackClient,
    _build_spec,
    _conn_factory,
    _plan,
    _seed_pending,
    _status,
)

_KEY = compute_idempotency_key(
    schedule_id="sched_src", root_run_id="run-src", emit_id="post"
)


def _worker_with_stub(factory, repo_root, stub) -> Worker:
    c = {"i": 0}

    def _eid() -> str:
        c["i"] += 1
        return f"evt-{c['i']:08d}-1111-1111-1111-111111111111"

    return Worker(
        conn_factory=factory,
        worker_id="worker-1",
        poll_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
        run_id_factory=lambda: "should-not-be-called",
        event_id_factory=_eid,
        slack_client=stub,
        repo_root=repo_root,
    )


def _events(factory, run_id="run-src"):
    conn = factory()
    try:
        rows = conn.execute(
            "SELECT kind, payload_json FROM events "
            "WHERE run_id = ? ORDER BY ts ASC",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return rows


def _kinds(factory, run_id="run-src"):
    return [k for k, _ in _events(factory, run_id)]


# ---------------------------------------------------------------------------
# B.3 — real source delivery → RUN_SUCCEEDED + keyed emit_succeeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_delivery_writes_keyed_emit_succeeded(tmp_path):
    factory, _db = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    _seed_pending(factory, spec=spec, plan=plan)
    stub = _StubSlackClient()
    w = _worker_with_stub(factory, tmp_path, stub)

    await w.tick()

    assert _status(factory) == "succeeded"
    assert len(stub.calls) == 1  # real delivery happened
    rows = _events(factory)
    kinds = [k for k, _ in rows]
    assert "emit_succeeded" in kinds
    assert "run_succeeded" in kinds
    import json

    es = next(json.loads(p) for k, p in rows if k == "emit_succeeded")
    assert es["idempotency_key"] == _KEY


# ---------------------------------------------------------------------------
# B.3 — dedup-skip: prior DURABLE keyed success ⇒ NO adapter call,
# emit_skipped_idempotent, run still succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_skip_no_adapter_call(tmp_path):
    factory, _db = _conn_factory(tmp_path)
    plan = _plan()
    spec = _build_spec(execution_plan_hash=plan.hash)
    _seed_pending(factory, spec=spec, plan=plan)

    # Pre-seed a DURABLE prior keyed emit_succeeded (a prior
    # attempt's marker) directly in the ledger.
    conn = factory()
    try:
        append_event(
            conn,
            Event(
                id="evt-prior-1111-1111-1111-111111111111",
                run_id=None,
                schedule_id="sched_src",
                ts=_NOW,
                kind=EventKind.EMIT_SUCCEEDED,
                payload={"worker_id": "w0", "idempotency_key": _KEY},
            ),
        )
        conn.commit()
    finally:
        conn.close()

    stub = _StubSlackClient()
    w = _worker_with_stub(factory, tmp_path, stub)

    await w.tick()

    assert _status(factory) == "succeeded"  # dedup-skip IS a success
    assert stub.calls == []  # adapter NEVER called
    kinds = _kinds(factory)
    assert "emit_skipped_idempotent" in kinds
    assert "run_succeeded" in kinds
    # NO second emit_succeeded written by this run.
    assert "emit_succeeded" not in kinds


# ---------------------------------------------------------------------------
# A.2 — OneOff is OUT of emit-dedup by construction
# ---------------------------------------------------------------------------


def test_oneoff_out_of_dedup_by_construction_A2():
    """A.2 — a OneOff fire does NOT compute a §6.4 key, does
    NOT consult prior_emit_succeeded, does NOT write a keyed
    marker (emit_marker_event=None, behaviour-identical to
    pre-phase-14). Pinned STRUCTURALLY: in
    ``_dispatch_emit_branch`` the idempotency calls appear
    ONLY inside the source-driven (``execution_plan_hash``)
    block — never on the OneOff/template path, which returns
    ``("succeeded", None)``. The BEHAVIOURAL regression pin
    is the EXISTING phase-9 OneOff success suite, UNMODIFIED
    + green (it asserts RUN_SUCCEEDED-only; a spurious marker
    would break it — B.2)."""
    import inspect

    src = inspect.getsource(Worker._dispatch_emit_branch)
    src_guard = src.index("if spec.execution_plan_hash is not None:")
    oneoff = src.index("# OneOffReminder emit branch.")
    # Both idempotency calls occur AFTER the source-driven
    # guard and BEFORE the OneOff branch — never on OneOff.
    for token in (
        "compute_idempotency_key(",
        "prior_emit_succeeded(",
    ):
        idx = src.index(token)
        assert src_guard < idx < oneoff, (
            f"{token} must be source-driven-only (A.1/A.2)"
        )
    # The OneOff branch returns no marker.
    assert 'return "succeeded", None' in src[oneoff:]


# ---------------------------------------------------------------------------
# B.1 / B.6 — _commit_success_atomic: both-or-neither
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_success_atomic_both_or_neither(tmp_path):
    from types import SimpleNamespace

    # _seed_pending inserts Run id="run-src" schedule_id="sched_src"
    # (PENDING) and returns None — _commit_success_atomic only
    # reads run.id, so a stand-in suffices.
    run = SimpleNamespace(id="run-src", schedule_id="sched_src")

    def _marker(run_id):
        return Event(
            id="evt-mk-0001-1111-1111-111111111111",
            run_id=run_id,
            schedule_id="sched_src",
            ts=_NOW,
            kind=EventKind.EMIT_SUCCEEDED,
            payload={"worker_id": "w", "idempotency_key": _KEY},
        )

    def _rs(run_id):
        return Event(
            id="evt-rs-0001-1111-1111-111111111111",
            run_id=run_id,
            schedule_id="sched_src",
            ts=_NOW,
            kind=EventKind.RUN_SUCCEEDED,
            payload={"worker_id": "w"},
        )

    # Happy: run row present (PENDING) → both marker +
    # run_succeeded persist in ONE TX.
    d1 = tmp_path / "ok"
    d1.mkdir()
    f1, _ = _conn_factory(d1)
    plan = _plan()
    _seed_pending(f1, spec=_build_spec(execution_plan_hash=plan.hash),
                  plan=plan)
    w1 = _worker_with_stub(f1, d1, _StubSlackClient())
    conn = f1()
    try:
        await w1._commit_success_atomic(
            conn=conn,
            run=run,
            completed_at=_NOW,
            run_succeeded_event=_rs("run-src"),
            emit_marker_event=_marker("run-src"),
        )
        conn.commit()
    finally:
        conn.close()
    k1 = _kinds(f1, "run-src")
    assert "emit_succeeded" in k1 and "run_succeeded" in k1

    # Rollback-all: a vanished run row (rowcount 0) raises and
    # NEITHER the marker nor run_succeeded persists (B.1/B.6
    # both-or-neither — durable IFF the run terminal-commits).
    # The marker is run_id=None (schedule-level) so the FK does
    # not fail BEFORE the rowcount guard — the guard is what we
    # are pinning.
    d2 = tmp_path / "rb"
    d2.mkdir()
    f2, _ = _conn_factory(d2)
    plan2 = _plan()
    _seed_pending(f2, spec=_build_spec(execution_plan_hash=plan2.hash),
                  plan=plan2)
    conn2 = f2()
    try:
        conn2.execute("DELETE FROM runs WHERE id = ?", ("run-src",))
        conn2.commit()
        with pytest.raises(RuntimeError):
            await w1._commit_success_atomic(
                conn=conn2,
                run=run,
                completed_at=_NOW,
                run_succeeded_event=_rs(None),
                emit_marker_event=_marker(None),
            )
        conn2.commit()
    finally:
        conn2.close()
    # Rolled back — NEITHER the marker NOR run_succeeded
    # persisted (both-or-neither).
    conn3 = f2()
    try:
        n = conn3.execute(
            "SELECT COUNT(*) FROM events WHERE id IN (?, ?)",
            (
                "evt-mk-0001-1111-1111-111111111111",
                "evt-rs-0001-1111-1111-111111111111",
            ),
        ).fetchone()[0]
    finally:
        conn3.close()
    assert n == 0
