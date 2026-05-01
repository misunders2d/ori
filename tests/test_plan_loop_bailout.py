"""Regression test for the plan-loop cascade bug (FBA→MSRP scheduling
incident, 2026-04-30).

When a scheduled task's session went over the 1M-token input limit, the
agent_executor returned a bail-out AgentResponse for context-limit, but
the outer plan loop ignored it and kept iterating against the poisoned
session — burning ~75-100 LLM calls and exhausting the paid-tier-2
input-token quota. The fix tags terminal AgentResponses with `error=...`
and the plan loop short-circuits when it sees one.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.agent_executor import AgentResponse
from app.tasks import _drive_plan_to_completion


@pytest.mark.asyncio
async def test_plan_loop_bails_on_terminal_error():
    """Bail-out response from extract_agent_response must short-circuit the
    plan loop, NOT trigger 25 retries against the same poisoned session."""
    poisoned = AgentResponse(text="⚠️ Context Limit Reached", error="context_limit")
    call_count = 0

    async def fake_extract(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return poisoned

    # plan_has_pending_steps must return True so without the bail-out the
    # loop would run all 25 iterations.
    with (
        patch("app.tools.planner.plan_has_pending_steps", return_value=True),
        patch("app.core.agent_executor.extract_agent_response", side_effect=fake_extract),
    ):
        response = await _drive_plan_to_completion(
            runner=None,
            user_id="system_scheduler",
            session_id="sched_test",
            first_response=AgentResponse(text="ok"),
            actual_caller_id="user@example.com",
            task_id="sched_test",
        )

    # Loop should run AT MOST once — the first iteration's response carries
    # the error and triggers the break. Without the fix, call_count was 25.
    assert call_count == 1, f"plan loop did not short-circuit: {call_count} retries"
    assert response.error == "context_limit"


@pytest.mark.asyncio
async def test_plan_loop_bails_immediately_if_first_response_errored():
    """If the first response (before any iteration) already carries an
    error, the loop should not run at all."""
    poisoned = AgentResponse(text="⚠️ Context Limit Reached", error="context_limit")
    extract_called = 0

    async def fake_extract(*args, **kwargs):
        nonlocal extract_called
        extract_called += 1
        return poisoned

    with (
        patch("app.tools.planner.plan_has_pending_steps", return_value=True),
        patch("app.core.agent_executor.extract_agent_response", side_effect=fake_extract),
    ):
        response = await _drive_plan_to_completion(
            runner=None,
            user_id="system_scheduler",
            session_id="sched_test",
            first_response=poisoned,  # already bailed
            actual_caller_id="user@example.com",
            task_id="sched_test",
        )

    assert extract_called == 0, "loop ran even though first_response had error"
    assert response.error == "context_limit"


@pytest.mark.asyncio
async def test_plan_loop_runs_normally_when_no_error():
    """Sanity check: a healthy response (error=None) keeps the loop going
    until plan_has_pending_steps returns False."""
    healthy = AgentResponse(text="step done", error=None)
    pending_calls = 0

    def fake_pending(_session_id):
        nonlocal pending_calls
        pending_calls += 1
        # Return True for first 3 checks, then False to end the loop
        return pending_calls <= 3

    extract_calls = 0

    async def fake_extract(*args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return healthy

    with (
        patch("app.tools.planner.plan_has_pending_steps", side_effect=fake_pending),
        patch("app.core.agent_executor.extract_agent_response", side_effect=fake_extract),
    ):
        response = await _drive_plan_to_completion(
            runner=None,
            user_id="user",
            session_id="sched_test",
            first_response=AgentResponse(text="initial", error=None),
            actual_caller_id=None,
            task_id="sched_test",
        )

    assert extract_calls == 3
    assert response.error is None
