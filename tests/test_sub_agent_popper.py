"""SubAgentPopperPlugin sets end_of_agent=True for sub-agents only.

ADK's _event_filter (runners.py, _find_agent_to_run) skips events with
end_of_agent=True. So if every sub-agent's after_agent_callback marks
its outgoing event as end_of_agent, the resume walk lands on the
coordinator's prior event instead of the sub-agent's, restoring the
parent as the active agent for the next turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.plugins.sub_agent_popper import SubAgentPopperPlugin


@pytest.fixture
def plugin():
    return SubAgentPopperPlugin()


@pytest.mark.asyncio
async def test_sets_end_of_agent_for_sub_agents(plugin):
    """Sub-agent (parent_agent set) → end_of_agent=True after callback."""
    sub_agent = SimpleNamespace(name="AmazonHeadAgent", parent_agent=object())
    actions = SimpleNamespace(end_of_agent=False)
    ctx = SimpleNamespace(actions=actions)

    result = await plugin.after_agent_callback(
        agent=sub_agent, callback_context=ctx,
    )

    assert result is None  # Plugin doesn't replace the agent's output
    assert ctx.actions.end_of_agent is True


@pytest.mark.asyncio
async def test_does_not_pop_root_coordinator(plugin):
    """Root agent (no parent) must NOT have end_of_agent set — that
    would prevent resume from finding the coordinator at all."""
    root = SimpleNamespace(name="CoordinatorAgent", parent_agent=None)
    actions = SimpleNamespace(end_of_agent=False)
    ctx = SimpleNamespace(actions=actions)

    await plugin.after_agent_callback(agent=root, callback_context=ctx)

    assert ctx.actions.end_of_agent is False  # unchanged


@pytest.mark.asyncio
async def test_callback_swallows_actions_mutation_failures(plugin):
    """If the actions object somehow can't be mutated, the plugin must
    not crash the agent invocation."""
    sub_agent = SimpleNamespace(name="Sub", parent_agent=object())

    class ImmutableActions:
        @property
        def end_of_agent(self):
            return False

        @end_of_agent.setter
        def end_of_agent(self, _value):
            raise AttributeError("immutable")

    ctx = SimpleNamespace(actions=ImmutableActions())

    # Should not raise.
    result = await plugin.after_agent_callback(
        agent=sub_agent, callback_context=ctx,
    )
    assert result is None


def test_marker_event_filter_logic_replicates_adk():
    """Replicate ADK's _event_filter to confirm an end_of_agent event is
    skipped during the resume walk — i.e. our pop signal works."""
    skipped = SimpleNamespace(
        author="AmazonAgent",
        actions=SimpleNamespace(end_of_agent=True, agent_state=None),
    )
    kept = SimpleNamespace(
        author="CoordinatorAgent",
        actions=SimpleNamespace(end_of_agent=False, agent_state=None),
    )

    def _event_filter(event):
        if event.author == "user":
            return False
        if event.actions.agent_state is not None or event.actions.end_of_agent:
            return False
        return True

    assert _event_filter(skipped) is False, (
        "popped sub-agent event must be skipped by ADK's resume walk"
    )
    assert _event_filter(kept) is True, (
        "coordinator event must remain visible to the resume walk"
    )
