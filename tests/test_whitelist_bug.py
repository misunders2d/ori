import os
import sys
from unittest.mock import patch

# Mock the environment BEFORE importing the module
with patch.dict(os.environ, {"ALLOWED_USER_IDS": "tg_admin"}):
    from app.core.whitelist import is_allowed, _whitelist, _load_data
    
    def test_is_allowed_bug():
        # Force reload with current env
        _load_data()
        
        assert is_allowed("tg_admin") is True
        assert is_allowed("tg_random") is False
        assert is_allowed("") is False
        assert is_allowed(None) is False
