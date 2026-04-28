"""Plan enforcement guarantees (ADK 2.0 native pattern):

The plan_executor workflow runs each step via `step_executor`, a
task-mode LlmAgent with `output_schema=StepResult`. The workflow reads
the typed status and routes:
  - 'completed' -> record summary, advance to next step
  - 'failed'    -> record reason, abandon plan, surface failure

These tests cover the failure-abort path, the happy path, and the
last-resort fallback when the agent returns something that doesn't
parse to StepResult.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.step_executor import StepResult
from app.runtime import plan_storage
from app.workflows import plan_executor


@pytest.fixture
def tmp_plan_db(tmp_path, monkeypatch):
    db = tmp_path / "plans.db"
    monkeypatch.setattr(plan_storage, "DB_PATH", str(db))
    return db


def _ctx_with_session(session_id: str):
    session = SimpleNamespace(id=session_id, session_id=session_id)
    return SimpleNamespace(session=session, run_node=AsyncMock())


@pytest.mark.asyncio
async def test_workflow_aborts_plan_when_judge_returns_failed(tmp_plan_db):
    """Each step is now executed by worker+judge. Sequence per step:
    ctx.run_node(worker) -> ctx.run_node(judge). The judge's typed
    StepResult drives the abort decision."""
    session_id = "test-session-fail"
    await plan_storage.seed_plan(session_id, "task", ["step a", "step b", "step c"])

    ctx = _ctx_with_session(session_id)
    # 4 calls expected: worker(a), judge(a-pass), worker(b), judge(b-fail)
    # then abort — worker(c) and judge(c) must NOT fire.
    ctx.run_node = AsyncMock(side_effect=[
        SimpleNamespace(text="worker a: ran BigQuery, 50 rows returned"),
        StepResult(status="completed", summary="BigQuery returned 50 rows"),
        SimpleNamespace(text="worker b: sheet write returned 401"),
        StepResult(
            status="failed",
            summary="attempted sheet write",
            failure_reason="sheet write returned 401 Unauthorized",
        ),
    ])
    initial_resp = SimpleNamespace(text="initial coordinator response")

    response = await plan_executor._drive_plan_loop(
        ctx, initial_resp, session_id,
    )

    assert ctx.run_node.await_count == 4, (
        f"expected 4 turns (worker+judge for steps a and b), "
        f"got {ctx.run_node.await_count}"
    )
    assert not await plan_storage.has_pending_steps(session_id)
    status = await plan_storage.get_plan_status(session_id)
    assert status["status"] == "abandoned"

    # On failure, the workflow returns a loud user-facing failure
    # message (types.Content), NOT the hollow worker_response. The
    # delivery layer extracts text from .parts; verify the message
    # contains the abort marker and the failure reason.
    parts = getattr(response, "parts", None) or []
    text = " ".join(p.text for p in parts if getattr(p, "text", None))
    assert "PLAN ABORTED" in text
    assert "401" in text


@pytest.mark.asyncio
async def test_workflow_runs_all_steps_when_judge_says_completed(tmp_plan_db):
    session_id = "test-session-ok"
    await plan_storage.seed_plan(session_id, "task", ["a", "b"])

    ctx = _ctx_with_session(session_id)
    ctx.run_node = AsyncMock(side_effect=[
        SimpleNamespace(text="worker a: did a thing"),
        StepResult(status="completed", summary="a done"),
        SimpleNamespace(text="worker b: did b thing"),
        StepResult(status="completed", summary="b done"),
    ])
    initial_resp = SimpleNamespace(text="initial")

    await plan_executor._drive_plan_loop(ctx, initial_resp, session_id)

    assert ctx.run_node.await_count == 4  # 2 steps × (worker + judge)
    assert not await plan_storage.has_pending_steps(session_id)
    status = await plan_storage.get_plan_status(session_id)
    assert status["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_aborts_when_judge_returns_unstructured(tmp_plan_db):
    """If step_judge returns garbage instead of a parseable StepResult,
    the coercer falls back to status='failed' and the loop aborts."""
    session_id = "test-session-unstruct"
    await plan_storage.seed_plan(session_id, "task", ["a", "b"])

    ctx = _ctx_with_session(session_id)
    # worker(a) returns text; judge(a) returns garbage instead of typed
    # StepResult — coercer treats as failed, loop aborts before step b.
    ctx.run_node = AsyncMock(side_effect=[
        SimpleNamespace(text="worker a: did something"),
        SimpleNamespace(text="not a valid StepResult, just prose"),
    ])
    initial_resp = SimpleNamespace(text="initial")

    await plan_executor._drive_plan_loop(ctx, initial_resp, session_id)

    assert ctx.run_node.await_count == 2, (
        "expected just worker+judge for step a — step b must not run"
    )
    status = await plan_storage.get_plan_status(session_id)
    assert status["status"] == "abandoned"


def test_coerce_step_result_handles_dict_form():
    """ADK 2.0 may surface output_schema results as a dict (per docs:
    'output_key stores dicts, not BaseModel instances'). The coercer
    must reconstruct the typed model from a dict."""
    raw = {"status": "completed", "summary": "did the thing", "failure_reason": None}
    response = SimpleNamespace(output=raw)
    result = plan_executor._coerce_step_result(response)
    assert isinstance(result, StepResult)
    assert result.status == "completed"
    assert result.summary == "did the thing"


def test_coerce_step_result_passthrough_for_already_typed():
    sr = StepResult(status="failed", summary="x", failure_reason="y")
    response = SimpleNamespace(output=sr)
    assert plan_executor._coerce_step_result(response) is sr
