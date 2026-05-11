"""Phase 3 unit tests: hard plan enforcement.

Covers:
- Plan schema extension (allowed_tools, must_call carry through create_plan
  and seed_plan).
- complete_step result validation.
- get_current_step_constraints returns the right thing for each plan state.
- plan_step_enforcer blocks out-of-plan tool calls + allows on-plan +
  exempts planner / scratchpad / transfer tools regardless of constraints.
"""

import os

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, sid: str = "plan-test-sess"):
        self.session_id = sid
        self.id = sid


class _FakeAgent:
    def __init__(self, name: str = "CoordinatorAgent"):
        self.name = name


class _FakeInvocationContext:
    def __init__(self, agent_name: str = "CoordinatorAgent"):
        self.agent = _FakeAgent(agent_name)


class _FakeToolContext:
    """Just enough of an ADK ToolContext for the planner + enforcer."""

    def __init__(self, session_id: str = "plan-test-sess"):
        self.session = _FakeSession(session_id)
        self._invocation_context = _FakeInvocationContext()
        # state is a plain dict for our purposes; planner only reads session_id
        self.state = {}


class _FakeTool:
    def __init__(self, name: str):
        self.name = name


@pytest.fixture
def isolated_plans_dir(tmp_path, monkeypatch):
    """Route plan storage to a tmp dir."""
    from app.tools import planner

    monkeypatch.setattr(planner, "_PLANS_DIR", str(tmp_path / "plans"))
    return tmp_path / "plans"


# ---------------------------------------------------------------------------
# Schema extension
# ---------------------------------------------------------------------------


def test_seed_plan_writes_constraints(isolated_plans_dir):
    from app.tools.planner import _load_plan, seed_plan

    seed_plan(
        "sess-A",
        task="Quarterly review",
        steps=["fetch", "summarize"],
        step_constraints=[
            {"allowed_tools": ["bigquery_*"], "must_call": []},
            {"allowed_tools": ["scratchpad_write"], "must_call": []},
        ],
    )

    plan = _load_plan("sess-A")
    assert plan["steps"][0]["allowed_tools"] == ["bigquery_*"]
    assert plan["steps"][1]["allowed_tools"] == ["scratchpad_write"]


def test_seed_plan_constraints_optional(isolated_plans_dir):
    from app.tools.planner import _load_plan, seed_plan

    seed_plan("sess-B", task="t", steps=["one", "two"])

    plan = _load_plan("sess-B")
    assert plan["steps"][0]["allowed_tools"] == []
    assert plan["steps"][1]["allowed_tools"] == []


def test_create_plan_writes_unconstrained_steps(isolated_plans_dir):
    """`create_plan` was historically allowed to set per-step constraints via a
    `step_constraints: list[dict]` kwarg. That parameter was removed because
    `list[dict] | None` produces a function-declaration schema Gemini rejects
    (nested `additional_properties` under `any_of[].items`) — see
    `tests/test_gemini_schema_compat.py` and the create_plan docstring. The
    scheduler still passes constraints through `seed_plan`; LLM-initiated
    plans are unconstrained at the tool-whitelist level.
    """
    from app.tools.planner import _load_plan, create_plan

    ctx = _FakeToolContext("sess-C")
    res = create_plan(task_description="t", steps=["a", "b"], tool_context=ctx)
    assert res["status"] == "success"

    plan = _load_plan("sess-C")
    assert plan["steps"][0]["allowed_tools"] == []
    assert plan["steps"][1]["allowed_tools"] == []


# ---------------------------------------------------------------------------
# complete_step result validation
# ---------------------------------------------------------------------------


def test_complete_step_rejects_empty_result(isolated_plans_dir):
    from app.tools.planner import complete_step, get_next_step, seed_plan

    seed_plan("sess-D", task="t", steps=["one"])
    ctx = _FakeToolContext("sess-D")
    get_next_step(ctx)

    res = complete_step("   ", ctx)
    assert res["status"] == "error"
    assert "non-empty" in res["message"]


def test_complete_step_accepts_valid_result(isolated_plans_dir):
    from app.tools.planner import complete_step, get_next_step, seed_plan

    seed_plan("sess-E", task="t", steps=["one"])
    ctx = _FakeToolContext("sess-E")
    get_next_step(ctx)

    res = complete_step("done — found 3 listings", ctx)
    assert res["status"] == "success"


# ---------------------------------------------------------------------------
# get_current_step_constraints
# ---------------------------------------------------------------------------


def test_current_step_constraints_returns_none_when_no_plan(isolated_plans_dir):
    from app.tools.planner import get_current_step_constraints

    assert get_current_step_constraints("nonexistent-sess") is None


def test_current_step_constraints_returns_none_when_unconstrained(isolated_plans_dir):
    from app.tools.planner import get_current_step_constraints, get_next_step, seed_plan

    seed_plan("sess-F", task="t", steps=["one"])  # no constraints
    get_next_step(_FakeToolContext("sess-F"))

    assert get_current_step_constraints("sess-F") is None


def test_current_step_constraints_returns_active_step_constraints(isolated_plans_dir):
    from app.tools.planner import get_current_step_constraints, get_next_step, seed_plan

    seed_plan(
        "sess-G",
        task="t",
        steps=["one"],
        step_constraints=[{"allowed_tools": ["bigquery_*", "scratchpad_write"]}],
    )
    get_next_step(_FakeToolContext("sess-G"))

    constraints = get_current_step_constraints("sess-G")
    assert constraints is not None
    assert constraints["allowed_tools"] == ["bigquery_*", "scratchpad_write"]
    assert constraints["step_id"] == 0


def test_current_step_constraints_none_after_completion(isolated_plans_dir):
    from app.tools.planner import (
        complete_step,
        get_current_step_constraints,
        get_next_step,
        seed_plan,
    )

    seed_plan(
        "sess-H",
        task="t",
        steps=["one"],
        step_constraints=[{"allowed_tools": ["keepa_*"]}],
    )
    ctx = _FakeToolContext("sess-H")
    get_next_step(ctx)
    complete_step("did it", ctx)

    # No more in_progress step → no active constraints.
    assert get_current_step_constraints("sess-H") is None


# ---------------------------------------------------------------------------
# plan_step_enforcer
# ---------------------------------------------------------------------------


def test_enforcer_passes_when_no_plan(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer

    ctx = _FakeToolContext("no-plan-sess")
    result = plan_step_enforcer(
        _FakeTool("slack_post_message"), {}, ctx
    )
    assert result is None


def test_enforcer_passes_when_unconstrained_step(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer
    from app.tools.planner import get_next_step, seed_plan

    seed_plan("sess-I", task="t", steps=["one"])  # no constraints
    ctx = _FakeToolContext("sess-I")
    get_next_step(ctx)

    result = plan_step_enforcer(_FakeTool("slack_post_message"), {}, ctx)
    assert result is None  # passes — soft enforcement only


def test_enforcer_blocks_off_plan_tool(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer
    from app.tools.planner import get_next_step, seed_plan

    seed_plan(
        "sess-J",
        task="t",
        steps=["fetch sales"],
        step_constraints=[{"allowed_tools": ["bigquery_*"]}],
    )
    ctx = _FakeToolContext("sess-J")
    get_next_step(ctx)

    result = plan_step_enforcer(_FakeTool("slack_post_message"), {}, ctx)
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert "Plan-step guardrail" in result["message"]
    assert "slack_post_message" in result["message"]


def test_enforcer_allows_matching_tool(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer
    from app.tools.planner import get_next_step, seed_plan

    seed_plan(
        "sess-K",
        task="t",
        steps=["fetch sales"],
        step_constraints=[{"allowed_tools": ["bigquery_*"]}],
    )
    ctx = _FakeToolContext("sess-K")
    get_next_step(ctx)

    result = plan_step_enforcer(_FakeTool("bigquery_query_table"), {}, ctx)
    assert result is None


def test_enforcer_allows_exempt_tools_regardless_of_constraints(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer
    from app.tools.planner import get_next_step, seed_plan

    seed_plan(
        "sess-L",
        task="t",
        steps=["one"],
        step_constraints=[{"allowed_tools": ["bigquery_*"]}],
    )
    ctx = _FakeToolContext("sess-L")
    get_next_step(ctx)

    for exempt in (
        "complete_step",
        "get_next_step",
        "abandon_plan",
        "scratchpad_read",
        "scratchpad_write",
        "transfer_to_agent",
    ):
        result = plan_step_enforcer(_FakeTool(exempt), {}, ctx)
        assert result is None, f"{exempt} should be exempt but was blocked"


def test_enforcer_glob_handles_star(isolated_plans_dir):
    from app.callbacks.guardrails import plan_step_enforcer
    from app.tools.planner import get_next_step, seed_plan

    seed_plan(
        "sess-M",
        task="t",
        steps=["one"],
        step_constraints=[{"allowed_tools": ["*"]}],  # any tool
    )
    ctx = _FakeToolContext("sess-M")
    get_next_step(ctx)

    # An arbitrary tool name is allowed.
    assert plan_step_enforcer(_FakeTool("anything_at_all"), {}, ctx) is None


def test_enforcer_pass_when_session_missing():
    """No session on the tool_context → enforcer can't look up plan → pass."""
    from app.callbacks.guardrails import plan_step_enforcer

    class _NoSessionCtx:
        session = None

    result = plan_step_enforcer(_FakeTool("anything"), {}, _NoSessionCtx())
    assert result is None
