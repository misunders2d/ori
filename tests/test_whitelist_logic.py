import os
import pytest
from app.runtime.perimeter import whitelist_chat, blacklist_chat, is_allowed, is_blacklisted, should_notify_admin, get_whitelist

def test_whitelist_basic():
    cid = "tg_test_123"
    # Ensure clean state
    if cid in get_whitelist():
        from app.runtime.perimeter import unwhitelist_chat
        unwhitelist_chat(cid)
        
    assert is_allowed(cid) is False
    whitelist_chat(cid)
    assert is_allowed(cid) is True
    
def test_blacklist_logic():
    cid = "tg_spam_999"
    blacklist_chat(cid)
    assert is_blacklisted(cid) is True
    assert is_allowed(cid) is False
    assert should_notify_admin(cid) is False

def test_notification_cooldown():
    cid = "tg_new_user"
    # First time should notify
    assert should_notify_admin(cid, cooldown=60) is True
    # Second time immediately after should not
    assert should_notify_admin(cid, cooldown=60) is False
