import os
from app.core.whitelist import _whitelist, _blacklist, get_whitelist

async def diagnose_live_state() -> dict:
    return {
        "whitelist_cache": list(_whitelist),
        "blacklist_cache": list(_blacklist),
        "admins_env": os.environ.get("ADMIN_USER_IDS"),
        "allowed_env": os.environ.get("ALLOWED_USER_IDS"),
    }
