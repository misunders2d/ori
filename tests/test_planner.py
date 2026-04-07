"""Tests for the plan-and-execute system."""

import json
import os
import pytest
from unittest.mock import MagicMock

from app.tools.planner import (
    create_plan,
    get_next_step,
    complete_step,
    get_plan_status,
    abandon_plan,
    get_active_plan_context,
    _PLANS_DIR,
)


def _make_ctx(session_id="test_session"):
    ctx = MagicMock()
    ctx.session.session_id = session_id
    ctx.session.id = session_id
    return ctx


@pytest.fixture(autouse=True)
def cleanup():
    """Remove test plan files before and after each test."""
    path = os.path.join(_PLANS_DIR, "test_session.json")
    if os.path.exists(path):
        os.remove(path)
    yield
    if os.path.exists(path):
        os.remove(path)


def test_create_plan():
    ctx = _make_ctx()
    result = create_plan("Test task", ["Step 1", "Step 2", "Step 3"], ctx)
    assert result["status"] == "success"
    assert "3 steps" in result["message"]
    assert len(result["plan"]["steps"]) == 3


def test_create_plan_empty_steps():
    ctx = _make_ctx()
    result = create_plan("Task", [], ctx)
    assert result["status"] == "error"


def test_create_plan_rejects_duplicate():
    ctx = _make_ctx()
    create_plan("Task 1", ["Step A"], ctx)
    result = create_plan("Task 2", ["Step B"], ctx)
    assert result["status"] == "error"
    assert "already active" in result["message"]


def test_get_next_step():
    ctx = _make_ctx()
    create_plan("Task", ["Step 1", "Step 2"], ctx)
    result = get_next_step(ctx)
    assert result["status"] == "success"
    assert result["current_step"]["id"] == 0
    assert result["current_step"]["description"] == "Step 1"
    assert "ONLY this step" in result["instruction"]


def test_get_next_step_no_plan():
    ctx = _make_ctx()
    result = get_next_step(ctx)
    assert result["status"] == "no_plan"


def test_complete_step():
    ctx = _make_ctx()
    create_plan("Task", ["Step 1", "Step 2"], ctx)
    get_next_step(ctx)
    result = complete_step("Done with step 1", ctx)
    assert result["status"] == "success"
    assert result["progress"] == "1/2"
    assert result["next_step_preview"] == "Step 2"


def test_complete_step_finishes_plan():
    ctx = _make_ctx()
    create_plan("Task", ["Only step"], ctx)
    get_next_step(ctx)
    result = complete_step("All done", ctx)
    assert result["status"] == "success"
    assert "All steps completed" in result["message"]


def test_full_plan_lifecycle():
    ctx = _make_ctx()
    create_plan("Full test", ["A", "B", "C"], ctx)

    for i, desc in enumerate(["A", "B"]):
        step = get_next_step(ctx)
        assert step["current_step"]["description"] == desc
        complete_step(f"Result {i}", ctx)

    # Last step — complete_step returns the final results
    step = get_next_step(ctx)
    assert step["current_step"]["description"] == "C"
    final = complete_step("Result 2", ctx)
    assert final["status"] == "success"
    assert "All steps completed" in final["message"]
    assert len(final["results"]) == 3


def test_get_plan_status():
    ctx = _make_ctx()
    create_plan("Status test", ["Step 1", "Step 2"], ctx)
    get_next_step(ctx)
    complete_step("Done", ctx)

    status = get_plan_status(ctx)
    assert status["progress"] == "1/2"
    assert "[x]" in status["checklist"][0]
    assert "[ ]" in status["checklist"][1]


def test_abandon_plan():
    ctx = _make_ctx()
    create_plan("Abandon test", ["Step 1", "Step 2"], ctx)
    result = abandon_plan(ctx)
    assert result["status"] == "success"
    assert "abandoned" in result["message"].lower()


def test_abandon_no_plan():
    ctx = _make_ctx()
    result = abandon_plan(ctx)
    assert result["status"] == "no_plan"


def test_get_active_plan_context_no_plan():
    assert get_active_plan_context("test_session") is None


def test_get_active_plan_context_with_step():
    ctx = _make_ctx()
    create_plan("Context test", ["Do something"], ctx)
    get_next_step(ctx)
    context = get_active_plan_context("test_session")
    assert context is not None
    assert "ACTIVE PLAN" in context
    assert "Do something" in context
    assert "ONLY" in context


def test_get_active_plan_context_between_steps():
    ctx = _make_ctx()
    create_plan("Between test", ["Step 1", "Step 2"], ctx)
    # No step started yet
    context = get_active_plan_context("test_session")
    assert context is not None
    assert "get_next_step" in context
