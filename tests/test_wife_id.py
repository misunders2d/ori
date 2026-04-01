from app.core.whitelist import is_allowed, reload
import os

def test_wife_id_not_allowed():
    # Force reload of current state
    reload()
    wife_id = "tg_185625742"
    allowed = is_allowed(wife_id)
    
    # We raise an error so the tool output shows the state
    admins = os.environ.get("ADMIN_USER_IDS", "")
    allowed_env = os.environ.get("ALLOWED_USER_IDS", "")
    
    raise AssertionError(f"Wife ({wife_id}) allowed: {allowed} | ADMINS: {admins} | ALLOWED: {allowed_env}")
