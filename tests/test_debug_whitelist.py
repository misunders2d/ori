import os
import json
from unittest.mock import patch

def test_debug_whitelist_logic():
    # Note: Using ADMIN_USER_IDS since ALLOWED_USER_IDS is now ignored.
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "tg_admin"}):
        from app.core.whitelist import is_allowed, _load_data, _whitelist, WHITELIST_PATH
        
        # Remove file if it exists to ensure clean state
        if os.path.exists(WHITELIST_PATH):
            os.remove(WHITELIST_PATH)
            
        _load_data()
        
        assert is_allowed("tg_admin") is True
