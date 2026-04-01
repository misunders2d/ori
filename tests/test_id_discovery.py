import os
def test_discovery():
    wife_id = "tg_185625742"
    admins = os.environ.get("ADMIN_USER_IDS", "")
    allowed = os.environ.get("ALLOWED_USER_IDS", "")
    res = ""
    if wife_id in admins: res += "ADMIN "
    if wife_id in allowed: res += "ALLOWED"
    if not res: res = "NONE"
    raise AssertionError(f"WIFE: {res}")
