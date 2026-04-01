import os
import sys
from unittest.mock import patch

# Force clear sys.modules for app.core.whitelist to ensure a fresh load
if "app.core.whitelist" in sys.modules:
    del sys.modules["app.core.whitelist"]

def test_is_allowed_bug():
    # Mock the environment BEFORE importing the module
    with patch.dict(os.environ, {"ALLOWED_USER_IDS": "tg_admin", "ADMIN_USER_IDS": ""}):
        from app.core.whitelist import is_allowed, reload
        
        # Ensure fresh state
        reload()
        
        assert is_allowed("tg_admin") is True
        assert is_allowed("tg_random") is False
        assert is_allowed("") is False
        assert is_allowed(None) is False
