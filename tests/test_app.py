"""App smoke tests — verifies the App wiring composes."""

from google.adk.agents import LlmAgent

from app.agent import PLUGINS, app, app_name, root_agent


def test_app_name_default():
    assert app_name == "ori"


def test_root_is_coordinator_agent():
    """Root is the Coordinator LlmAgent directly, replicating the legacy
    amazon_manager flow. Earlier the rebuild used a Workflow wrapper with a
    plan-completion-loop edge — that produced re-firing bugs (cancelled
    tasks generating extra tool calls). Single Agent root, single response
    per user message, plan continuation handled by run_scheduled_task only.
    """
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "CoordinatorAgent"


def test_app_root_agent_matches():
    assert app.root_agent is root_agent


def test_plugin_order_canonical():
    """Plugin order is meaningful — first plugin to short-circuit wins."""
    expected = [
        "perimeter",
        "admin_gate",
        "state_initializer",
        "model_config",
        "prompt_injection",
        "plan_enforcer",
        "a2a_privacy",
        "output_sanitizer",
        "verify_retry",
        "binary_content_scanner",
        "model_error_handler",
    ]
    assert [p.name for p in PLUGINS] == expected
    assert [p.name for p in app.plugins] == expected


def test_app_has_resumability_for_oauth():
    assert app.resumability_config is not None
    assert app.resumability_config.is_resumable is True


def test_app_has_events_compaction():
    assert app.events_compaction_config is not None
    assert app.events_compaction_config.compaction_interval == 10
    assert app.events_compaction_config.overlap_size == 3


def test_workflow_no_state_schema():
    """state_schema is intentionally NOT attached to the Workflow.

    ADK 2.0's state validator (sessions/state.py:_validate_state_entry) is
    strict about declared keys, but its own SkillToolset writes dynamic
    keys like `_adk_activated_skill_<AgentName>` (skill_toolset.py:166).
    Attaching state_schema makes ADK crash on its own internal writes.

    See app/workflows/plan_executor.py for the design note. OriSessionState
    is kept as documentation/IDE help but not passed to the Workflow.
    """
    assert root_agent.state_schema is None
