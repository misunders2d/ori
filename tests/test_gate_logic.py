import os
import pytest
from unittest.mock import MagicMock, patch
from app.core.whitelist import whitelist_chat, is_allowed, reload, _whitelist

def test_gate_logic_user_vs_chat():
    # Setup: whitelist a group but not a user
    group_id = "tg_group_123"
    user_id = "tg_user_456"
    admin_id = "tg_admin_789"
    
    with patch.dict(os.environ, {"ALLOWED_USER_IDS": admin_id}):
        reload()
        whitelist_chat(group_id)
        
        # Test 1: User is not whitelisted, but group is. 
        # In our new strict logic, is_allowed(user_id) should be checked for interaction.
        assert is_allowed(admin_id) is True
        assert is_allowed(group_id) is True
        assert is_allowed(user_id) is False
        
        # The poller logic (which we'll simulate here) should block the user
        # if not is_allowed(user_id): ... continue
        
def test_whitelist_reload_after_env_change():
    with patch.dict(os.environ, {"ALLOWED_USER_IDS": "tg_a"}):
        reload()
        assert is_allowed("tg_a") is True
        assert is_allowed("tg_b") is False
        
    with patch.dict(os.environ, {"ALLOWED_USER_IDS": "tg_b"}):
        reload()
        assert is_allowed("tg_a") is False
        assert is_allowed("tg_b") is True
