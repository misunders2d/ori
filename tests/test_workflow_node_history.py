"""Guard: any LlmAgent used as a Workflow node must have mode='chat'.

ADK 2.0 quirk (google.adk.workflow._llm_agent_wrapper.run_llm_agent_as_node):
when an LlmAgent runs as a workflow node and `mode` is unset, it defaults to
'single_turn', which sets `include_contents='none'` — silently strips all
conversation history. The agent sees only the latest message and has no
recall of prior turns.

For a chat-style agent at the workflow root (the coordinator), we must set
mode='chat' explicitly so include_contents stays 'default' and the LLM sees
the full session events.
"""

from app.workflows.plan_executor import plan_executor_workflow


def test_coordinator_node_has_chat_mode():
    """The coordinator node must have mode='chat' (not 'single_turn')
    so it preserves conversation history across turns."""
    from app.agents.coordinator import root_agent as coordinator
    assert coordinator.mode == "chat", (
        f"CoordinatorAgent runs as a Workflow node and must use mode='chat'. "
        f"Got mode={coordinator.mode!r}. Without 'chat', ADK 2.0 forces "
        f"include_contents='none', stripping all conversation history."
    )
    assert coordinator.include_contents == "default", (
        f"CoordinatorAgent.include_contents={coordinator.include_contents!r}. "
        f"Must be 'default' for the agent to see session history."
    )


def test_workflow_root_is_constructable():
    """Smoke check: the plan_executor workflow imports and constructs."""
    assert plan_executor_workflow is not None
    assert plan_executor_workflow.name == "ori_plan_executor"
