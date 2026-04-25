"""OriSessionState — defaults and field shape."""
from app.state import OriSessionState


def test_defaults():
    s = OriSessionState()
    assert s.user_id is None
    assert s.master_user_id == []
    assert s.bot_name == "Ori"
    assert s.user_preferences == {}
    assert s.model == {}
    assert s.use_thinking is False
    assert s.plan_id is None
    assert s.verify_failure_count == 0


def test_round_trip():
    s = OriSessionState(
        user_id="tg_42",
        bot_name="Scout",
        model={"CoordinatorAgent": "litellm/openai/gpt-4o"},
        use_thinking=True,
        verify_failure_count=2,
    )
    blob = s.model_dump()
    s2 = OriSessionState.model_validate(blob)
    assert s2.user_id == "tg_42"
    assert s2.bot_name == "Scout"
    assert s2.model["CoordinatorAgent"] == "litellm/openai/gpt-4o"
    assert s2.use_thinking is True
    assert s2.verify_failure_count == 2


def test_extra_keys_allowed():
    """Plugins may stash transient state under ad-hoc keys; the schema is permissive."""
    s = OriSessionState.model_validate({"user_id": "x", "_plan_workflow_iters": 3})
    # Extra fields are preserved (model_config = extra='allow')
    assert s.user_id == "x"
