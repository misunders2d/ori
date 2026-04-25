"""App + workflow smoke tests — verifies the full Phase F wiring composes."""

from google.adk.workflow import Workflow

from app.agent import PLUGINS, app, app_name, root_agent


def test_app_name_default():
    assert app_name == "ori"


def test_root_is_workflow():
    assert isinstance(root_agent, Workflow)
    assert root_agent.name == "ori_plan_executor"


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


def test_workflow_state_schema_attached():
    """state_schema lives on the Workflow (root_agent), not on App."""
    from app.state import OriSessionState
    assert root_agent.state_schema is OriSessionState
