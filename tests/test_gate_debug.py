import os
from app.core.whitelist import is_allowed, reload

def test_gate_debug():
    # Use print with specific prefix to find in logs
    print(f"\nDEBUG_GATE_START")
    reload()
    wife_id = "tg_185625742"
    allowed = is_allowed(wife_id)
    admins = os.environ.get("ADMIN_USER_IDS", "")
    print(f"DEBUG_GATE_WIFE_ALLOWED: {allowed}")
    print(f"DEBUG_GATE_ADMINS: {admins}")
    print(f"DEBUG_GATE_END")
    assert True
