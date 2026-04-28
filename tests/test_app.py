"""App smoke tests — verifies the App wiring composes."""

from app.agent import PLUGINS, app, app_name, root_agent


def test_app_name_default():
    assert app_name == "ori"


def test_root_is_coordinator():
    """Root is the coordinator LlmAgent. The previous workflow-wrapping
    approach was reverted because ADK 2.0's ctx.run_node boundary
    swallowed output for chat-mode agents (returned None) and was
    unreliable for task-mode agents that delegate via transfer_to_agent.

    Plan enforcement is now done via PlanEnforcerPlugin (injects active
    plan into system_instruction every turn) plus tasks.py's
    `_drive_plan_to_completion` (re-invokes runner with continuation
    prompt while pending steps remain).
    """
    assert root_agent.name == "CoordinatorAgent"


def test_app_root_agent_matches():
    assert app.root_agent is root_agent


def test_plugin_order_canonical():
    """Plugin order is meaningful — first plugin to short-circuit wins.
    plan_enforcer sits after state_initializer (which sets state.session_id)
    and after prompt_injection (which runs the system-directive injection)."""
    expected = [
        "perimeter",
        "admin_gate",
        "state_initializer",
        "model_config",
        "prompt_injection",
        "plan_enforcer",
        "reflect_retry_tool_plugin",
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
