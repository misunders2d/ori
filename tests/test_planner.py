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
    seed_plan,
    _PLANS_DIR,
)
from app.tools.scheduling import _validate_steps


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


# ---------------------------------------------------------------------------
# seed_plan — programmatic plan seeding (scheduler-driven enforcement)
# ---------------------------------------------------------------------------

def test_seed_plan_populates_storage():
    """Plan written by seed_plan must be visible to get_active_plan_context."""
    seed_plan("test_session", "Daily ASIN task", ["Fetch listing", "Compare", "Report"])
    context = get_active_plan_context("test_session")
    assert context is not None
    # Fresh seed: no step in_progress yet — context should flag ACTIVE PLAN
    # and prompt the agent to call get_next_step (which plan_enforcer injects
    # into every turn until the agent progresses).
    assert "ACTIVE PLAN" in context
    assert "Daily ASIN task" in context
    assert "get_next_step" in context


def test_seed_plan_empty_steps_is_noop():
    """seed_plan with None or [] should not create any file."""
    seed_plan("test_session", "No-op", None)
    assert get_active_plan_context("test_session") is None
    seed_plan("test_session", "No-op", [])
    assert get_active_plan_context("test_session") is None


def test_seed_plan_overwrites_existing():
    """A subsequent seed_plan must replace the prior plan for the same session."""
    seed_plan("test_session", "First", ["A", "B"])
    seed_plan("test_session", "Second", ["X", "Y", "Z"])
    # Progress through to first step so we can inspect step description
    ctx = _make_ctx()
    step = get_next_step(ctx)
    # Must reflect the second plan (X), not the first (A)
    assert step["current_step"]["description"] == "X"


def test_seed_plan_integrates_with_get_next_step():
    """After seed_plan, the agent's normal get_next_step tool must see the plan."""
    seed_plan("test_session", "Task", ["Alpha", "Beta"])
    ctx = _make_ctx()
    result = get_next_step(ctx)
    assert result["status"] == "success"
    assert result["current_step"]["description"] == "Alpha"


def test_seed_plan_strips_not_needed():
    """Steps are stored as given; no paraphrasing or normalization."""
    exact_step = "Step one: CHECK INVENTORY BELOW 30 UNITS (don't round)"
    seed_plan("test_session", "T", [exact_step])
    ctx = _make_ctx()
    result = get_next_step(ctx)
    assert result["current_step"]["description"] == exact_step


# ---------------------------------------------------------------------------
# _validate_steps — scheduling tool input validation
# ---------------------------------------------------------------------------

def test_validate_steps_none_returns_none():
    assert _validate_steps(None) is None


def test_validate_steps_empty_list_returns_none():
    assert _validate_steps([]) is None


def test_validate_steps_valid_list():
    result = _validate_steps(["one", "two", " three "])
    assert result == ["one", "two", "three"]


def test_validate_steps_rejects_non_list():
    result = _validate_steps("not a list")
    assert isinstance(result, dict) and result["status"] == "error"
    result = _validate_steps({"a": 1})
    assert isinstance(result, dict) and result["status"] == "error"


def test_validate_steps_rejects_non_string_entry():
    result = _validate_steps(["ok", 42, "also ok"])
    assert isinstance(result, dict) and result["status"] == "error"


def test_validate_steps_rejects_empty_string_entry():
    result = _validate_steps(["ok", "   ", "also ok"])
    assert isinstance(result, dict) and result["status"] == "error"


def test_validate_steps_all_whitespace_becomes_none():
    """A list that's entirely empty strings after stripping should be treated as no steps."""
    # After removing the empty-string entry (validation would reject it),
    # entries that are just whitespace are also rejected.
    result = _validate_steps(["   "])
    assert isinstance(result, dict) and result["status"] == "error"
