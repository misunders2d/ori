import os
import json
from app.core.whitelist import is_allowed, reload

def check_wife():
    wife_id = "tg_185625742"
    reload()
    allowed = is_allowed(wife_id)
    admins = os.environ.get("ADMIN_USER_IDS")
    allowed_ids = os.environ.get("ALLOWED_USER_IDS")
    
    with open("data/wife_debug.json", "w") as f:
        json.dump({
            "wife_id": wife_id,
            "allowed": allowed,
            "ADMIN_USER_IDS": admins,
            "ALLOWED_USER_IDS": allowed_ids
        }, f, indent=2)
    return "Debug file data/wife_debug.json written."

if __name__ == "__main__":
    print(check_wife())
