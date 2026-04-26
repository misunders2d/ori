from app.runtime.pending_actions import (
    cleanup_expired,
    get_and_delete_action,
    stage_action,
)


def test_stage_and_retrieve_action():
    tool_name = "update_self"
    args = {"foo": "bar"}
    user_id = "user1"
    session_id = "sess1"

    token = stage_action(tool_name, args, user_id, session_id)
    assert token.startswith("ACT-")

    action = get_and_delete_action(token)
    assert action is not None
    assert action["tool_name"] == tool_name
    assert action["args"] == args
    assert action["user_id"] == user_id

    # Single-use: retrieving again should fail
    action2 = get_and_delete_action(token)
    assert action2 is None

def test_expired_action():
    tool_name = "session_refresh"
    args = {"mode": "fresh"}
    user_id = "user1"
    session_id = "sess1"

    # Stage with 0 TTL (expires immediately)
    token = stage_action(tool_name, args, user_id, session_id, ttl_minutes=-1)

    action = get_and_delete_action(token)
    assert action is None

def test_cleanup_expired():
    stage_action("test", {}, "u", "s", ttl_minutes=-1)
    cleanup_expired()
    # No easy way to check without direct DB access or token, but this ensures it runs
