import os
import sys
from unittest.mock import patch

# Force clear sys.modules for app.runtime.perimeter to ensure a fresh load
if "app.runtime.perimeter" in sys.modules:
    del sys.modules["app.runtime.perimeter"]

def test_is_allowed_bug():
    # Note: Using ADMIN_USER_IDS since ALLOWED_USER_IDS is now ignored.
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "tg_admin"}):
        from app.runtime.perimeter import is_allowed, reload

        # Ensure fresh state
        reload()

        assert is_allowed("tg_admin") is True
        assert is_allowed("tg_random") is False
        assert is_allowed("") is False
        assert is_allowed(None) is False
