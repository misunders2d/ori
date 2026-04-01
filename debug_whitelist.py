import os
import json
from unittest.mock import patch

# Clear environment and mock it
with patch.dict(os.environ, {"ALLOWED_USER_IDS": "tg_admin", "ADMIN_USER_IDS": ""}):
    from app.core.whitelist import is_allowed, _load_data, _whitelist, WHITELIST_PATH
    
    # Remove file if it exists to ensure clean state
    if os.path.exists(WHITELIST_PATH):
        os.remove(WHITELIST_PATH)
        
    _load_data()
    print(f"Whitelist content: {_whitelist}")
    print(f"is_allowed('tg_admin'): {is_allowed('tg_admin')}")
    
    assert is_allowed("tg_admin") is True
    print("Assertion passed!")
