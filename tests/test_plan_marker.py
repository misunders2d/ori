"""Verifies _stamp_coordinator_active appends a coordinator-authored
marker event that ADK 2.0's _find_agent_to_run will pick up.

Without this marker, plan-continuation prompts route to whichever
sub-agent was last active (BigQueryAgent / AmazonAgent), which doesn't
have the planner tools (get_next_step / complete_step / abandon_plan)
and errors with `Tool 'X' not found`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import _stamp_coordinator_active


@pytest.mark.asyncio
async def test_stamp_appends_event_authored_by_coordinator():
    """The appended marker must:
    - have author == CoordinatorAgent (matches root_agent.name)
    - have empty actions (no end_of_agent / agent_state — those would
      cause _event_filter to drop it from the resume walk)
    - have content=None (no LLM payload; pure routing nudge)
    """
    appended_events: list = []

    async def fake_get_session(app_name, user_id, session_id):
        return SimpleNamespace(id=session_id, events=[])

    async def fake_append_event(session, event):
        appended_events.append(event)
        return event

    runner = SimpleNamespace(
        app_name="ori",
        session_service=SimpleNamespace(
            get_session=fake_get_session,
            append_event=fake_append_event,
        ),
    )

    await _stamp_coordinator_active(runner, "system_scheduler", "sched_test")

    assert len(appended_events) == 1
    ev = appended_events[0]
    assert ev.author == "CoordinatorAgent"
    assert ev.content is None
    assert ev.actions.end_of_agent is None
    assert ev.actions.agent_state is None
    assert ev.invocation_id.startswith("plan_continuation_marker_")


@pytest.mark.asyncio
async def test_stamp_silently_no_ops_when_session_missing():
    """If get_session returns None (session expired / never existed),
    don't crash — just skip. tasks.py's outer try/except handles the
    actual scheduled-task failure surface; the marker is best-effort."""
    runner = SimpleNamespace(
        app_name="ori",
        session_service=SimpleNamespace(
            get_session=AsyncMock(return_value=None),
            append_event=AsyncMock(),
        ),
    )

    # Should not raise.
    await _stamp_coordinator_active(runner, "system_scheduler", "missing")

    runner.session_service.append_event.assert_not_called()


def test_marker_event_passes_adk_event_filter():
    """The marker event must satisfy ADK's _event_filter (defined inside
    runners.py:_find_agent_to_run) — otherwise the resume walk skips it
    and we're back to picking the sub-agent.

    The filter (verbatim from google.adk.runners.Runner._find_agent_to_run):
        if event.author == 'user': return False
        if event.actions.agent_state is not None or event.actions.end_of_agent: return False
        return True
    """
    import uuid

    from google.adk.events.event import Event, EventActions

    ev = Event(
        author="CoordinatorAgent",
        actions=EventActions(),
        invocation_id=f"plan_continuation_marker_{uuid.uuid4().hex[:8]}",
    )

    # Replicate ADK's _event_filter and assert pass.
    def _event_filter(event):
        if event.author == "user":
            return False
        if event.actions.agent_state is not None or event.actions.end_of_agent:
            return False
        return True

    assert _event_filter(ev), "marker event must pass ADK's resume-walk filter"

    # And the resume walk's first check: event.author == root_agent.name → return root.
    # We replicate that match:
    assert ev.author == "CoordinatorAgent", (
        "marker author must match root_agent.name so _find_agent_to_run returns root"
    )
