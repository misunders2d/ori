import os
import sys
from unittest.mock import patch

# Force fresh reload of whitelist module
if "app.runtime.perimeter" in sys.modules:
    del sys.modules["app.runtime.perimeter"]

def test_gate_logic_user_vs_chat():
    from app.runtime.perimeter import is_allowed, reload, whitelist_chat

    # Setup: whitelist a group but not a user
    group_id = "tg_group_123"
    user_id = "tg_user_456"
    admin_id = "tg_admin_789"

    with patch.dict(os.environ, {"ADMIN_USER_IDS": admin_id}):
        reload()
        # Clean state for test
        from app.runtime.perimeter import WHITELIST_PATH, _whitelist
        _whitelist.clear()
        _whitelist.add(admin_id)
        if os.path.exists(WHITELIST_PATH):
            os.remove(WHITELIST_PATH)

        whitelist_chat(group_id)

        # In the core whitelist module, IDs are just strings.
        assert is_allowed(admin_id) is True
        assert is_allowed(group_id) is True
        assert is_allowed(user_id) is False

def test_whitelist_reload_after_env_change():
    from app.runtime.perimeter import is_allowed, reload

    # Note: ALLOWED_USER_IDS is now ignored for security. We use ADMIN_USER_IDS.
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "tg_a"}):
        reload()
        assert is_allowed("tg_a") is True
        assert is_allowed("tg_b") is False

    with patch.dict(os.environ, {"ADMIN_USER_IDS": "tg_b"}):
        reload()
        assert is_allowed("tg_a") is False
        assert is_allowed("tg_b") is True
