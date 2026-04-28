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
async def test_workflow_aborts_plan_on_step_failed_typed_output(tmp_plan_db):
    """Step b returns StepResult(status='failed'). The workflow must
    abandon the plan and never request step c."""
    session_id = "test-session-fail"
    await plan_storage.seed_plan(session_id, "task", ["step a", "step b", "step c"])

    ctx = _ctx_with_session(session_id)
    ctx.run_node = AsyncMock(side_effect=[
        StepResult(status="completed", summary="did step a"),
        StepResult(
            status="failed",
            summary="tried sheet write",
            failure_reason="sheet write returned 401 Unauthorized",
        ),
        # step c must never be requested.
    ])
    initial_resp = SimpleNamespace(text="initial coordinator response")

    response = await plan_executor._drive_plan_loop(
        ctx, initial_resp, session_id,
    )

    assert ctx.run_node.await_count == 2, (
        "expected 2 step turns (a + b) — c must not run after b failed"
    )
    assert not await plan_storage.has_pending_steps(session_id)

    status = await plan_storage.get_plan_status(session_id)
    assert status["status"] == "abandoned"

    # The returned response is the failing step's result so the caller
    # surfaces the actual failure to the user.
    coerced = plan_executor._coerce_step_result(response)
    assert coerced.status == "failed"
    assert "401" in (coerced.failure_reason or "")


@pytest.mark.asyncio
async def test_workflow_runs_to_completion_when_all_steps_pass(tmp_plan_db):
    session_id = "test-session-ok"
    await plan_storage.seed_plan(session_id, "task", ["a", "b"])

    ctx = _ctx_with_session(session_id)
    ctx.run_node = AsyncMock(side_effect=[
        StepResult(status="completed", summary="did a"),
        StepResult(status="completed", summary="did b"),
    ])
    initial_resp = SimpleNamespace(text="initial")

    await plan_executor._drive_plan_loop(ctx, initial_resp, session_id)

    assert ctx.run_node.await_count == 2
    assert not await plan_storage.has_pending_steps(session_id)
    status = await plan_storage.get_plan_status(session_id)
    assert status["status"] == "completed"


@pytest.mark.asyncio
async def test_workflow_aborts_when_step_executor_returns_unstructured(tmp_plan_db):
    """If step_executor returns plain text instead of a StepResult (e.g.
    the model hallucinated free text past the schema), treat it as a
    failure. Better to abort than march through with an unrecognized
    response shape."""
    session_id = "test-session-unstruct"
    await plan_storage.seed_plan(session_id, "task", ["a", "b"])

    ctx = _ctx_with_session(session_id)
    # First step returns garbage. Second step would only fire if the
    # workflow ignored the bad shape — it must NOT fire.
    ctx.run_node = AsyncMock(side_effect=[
        SimpleNamespace(text="oh sure I did the thing"),
        StepResult(status="completed", summary="b"),
    ])
    initial_resp = SimpleNamespace(text="initial")

    await plan_executor._drive_plan_loop(ctx, initial_resp, session_id)

    assert ctx.run_node.await_count == 1, (
        "an unstructured response must abort — second step must not run"
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
