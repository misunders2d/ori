"""Agent-facing planner tools — thin wrappers over runtime/plan_storage.

The storage-layer behavior is fully exercised in test_plan_storage.py.
Here we just verify the tool wrappers translate tool_context.session
correctly and surface the right shape back to the agent.
"""

from unittest.mock import MagicMock

import pytest

from app.runtime import plan_storage
from app.tools.planner import (
    abandon_plan,
    complete_step,
    create_plan,
    get_next_step,
    get_plan_status,
)


@pytest.fixture
def tmp_plan_db(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_storage, "DB_PATH", str(tmp_path / "plans.db"))


def _ctx(session_id: str = "tg_chat_1") -> MagicMock:
    sess = MagicMock()
    sess.session_id = session_id
    sess.id = session_id
    tc = MagicMock()
    tc.session = sess
    return tc


@pytest.mark.asyncio
async def test_create_plan_round_trip(tmp_plan_db):
    out = await create_plan("build x", ["a", "b"], tool_context=_ctx())
    assert out["status"] == "success"
    assert out["step_count"] == 2


@pytest.mark.asyncio
async def test_create_plan_requires_session_id(tmp_plan_db):
    """No session_id on the tool_context — error rather than silent miss."""
    bad = MagicMock()
    bad.session = None
    out = await create_plan("x", ["a"], tool_context=bad)
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_create_plan_empty_steps_rejected(tmp_plan_db):
    out = await create_plan("x", [], tool_context=_ctx())
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_get_next_step_after_create(tmp_plan_db):
    await create_plan("x", ["alpha", "beta"], tool_context=_ctx())
    out = await get_next_step(tool_context=_ctx())
    assert out["status"] == "success"
    assert out["step_index"] == 0
    assert out["description"] == "alpha"
    assert "complete_step" in out["instruction"]


@pytest.mark.asyncio
async def test_complete_step_advances(tmp_plan_db):
    await create_plan("x", ["alpha", "beta"], tool_context=_ctx())
    await get_next_step(tool_context=_ctx())
    out = await complete_step("alpha-result", tool_context=_ctx())
    assert out["status"] == "success"
    assert out["next_step"]["description"] == "beta"


@pytest.mark.asyncio
async def test_full_lifecycle(tmp_plan_db):
    """create -> step1 -> complete -> step2 -> complete -> "all completed"."""
    ctx = _ctx()
    await create_plan("x", ["a", "b"], tool_context=ctx)
    await get_next_step(tool_context=ctx)
    await complete_step("ra", tool_context=ctx)
    await get_next_step(tool_context=ctx)
    last = await complete_step("rb", tool_context=ctx)
    assert "All steps completed" in last["message"]


@pytest.mark.asyncio
async def test_get_plan_status_returns_structured(tmp_plan_db):
    ctx = _ctx()
    await create_plan("x", ["a", "b"], tool_context=ctx)
    out = await get_plan_status(tool_context=ctx)
    assert out["status"] == "success"
    assert out["plan"]["task"] == "x"
    assert len(out["plan"]["steps"]) == 2


@pytest.mark.asyncio
async def test_abandon_plan(tmp_plan_db):
    ctx = _ctx()
    await create_plan("x", ["a"], tool_context=ctx)
    out = await abandon_plan(tool_context=ctx)
    assert out["status"] == "success"
    # Second abandon: no-op.
    out2 = await abandon_plan(tool_context=ctx)
    assert "no active plan" in out2["message"].lower()
