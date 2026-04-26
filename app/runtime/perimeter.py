import json
import logging
import os
import time

# Use a specific logger that is guaranteed to show up
logger = logging.getLogger("GATE_AUDIT")
logger.setLevel(logging.INFO)

WHITELIST_PATH = os.path.abspath("./data/whitelist.json")
BLACKLIST_PATH = os.path.abspath("./data/blacklist.json")

# Persistent memory cache
_whitelist = set()
_blacklist = set()
_last_notified = {}  # chat_id -> timestamp

def _load_data():
    global _whitelist, _blacklist
    new_whitelist = set()
    new_blacklist = set()

    # 1. ADMINS ALWAYS ALLOWED (From Env)
    # We strictly only trust the Admin IDs for infrastructure bypass
    env_admins = os.environ.get("ADMIN_USER_IDS", "").split(",")
    for uid in env_admins:
        clean_uid = uid.strip()
        if clean_uid:
            new_whitelist.add(clean_uid)
            # Compatibility: if it's a numeric ID, also add the tg_ prefixed version
            if clean_uid.isdigit():
                new_whitelist.add(f"tg_{clean_uid}")
            print(f"Gate Config: Admin {clean_uid} authorized.")

    # 2. LOAD WHITELIST FROM JSON
    # This is the ONLY other source of truth. ALLOWED_USER_IDS is now ignored for safety.
    if os.path.exists(WHITELIST_PATH):
        try:
            with open(WHITELIST_PATH) as f:
                data = json.load(f)
                for uid in (data if isinstance(data, list) else data.keys() if isinstance(data, dict) else []):
                    clean_uid = str(uid).strip()
                    if clean_uid:
                        new_whitelist.add(clean_uid)
        except Exception as e:
            print(f"Gate Error: Failed to load whitelist.json: {e}")

    # 3. LOAD BLACKLIST FROM JSON
    if os.path.exists(BLACKLIST_PATH):
        try:
            with open(BLACKLIST_PATH) as f:
                data = json.load(f)
                for uid in (data if isinstance(data, list) else data.keys() if isinstance(data, dict) else []):
                    clean_uid = str(uid).strip()
                    if clean_uid:
                        new_blacklist.add(clean_uid)
        except Exception as e:
            print(f"Gate Error: Failed to load blacklist.json: {e}")

    _whitelist.clear()
    _whitelist.update(new_whitelist)
    _blacklist.clear()
    _blacklist.update(new_blacklist)

    print(f"Gate Config: Perimeter Locked. {len(_whitelist)} total authorized IDs.")

def reload():
    """Manual reload of security data."""
    _load_data()

def is_allowed(chat_id: str) -> bool:
    """The Gatekeeper. Strict Fail-Closed."""
    if not chat_id:
        return False

    chat_id_str = str(chat_id).strip()

    # Check whitelist cache (direct match)
    if chat_id_str in _whitelist:
        logger.info(f"Gate: Access GRANTED for {chat_id_str}")
        return True

    # Robustness check: if we have the prefixed version in whitelist but checking the raw ID
    if chat_id_str.isdigit() and f"tg_{chat_id_str}" in _whitelist:
        logger.info(f"Gate: Access GRANTED for {chat_id_str} (via tg_ prefix)")
        return True

    # Robustness check: if we have the raw ID in whitelist but checking the prefixed version
    if chat_id_str.startswith("tg_") and chat_id_str[3:].isdigit() and chat_id_str[3:] in _whitelist:
        logger.info(f"Gate: Access GRANTED for {chat_id_str} (via raw ID)")
        return True

    logger.warning(f"Gate: Access DENIED for {chat_id_str} (Unauthorized)")
    return False

def is_blacklisted(chat_id: str) -> bool:
    if not chat_id:
        return False
    return str(chat_id) in _blacklist

def whitelist_chat(chat_id: str):
    if not chat_id:
        return
    chat_id = str(chat_id).strip()
    _whitelist.add(chat_id)
    if chat_id in _blacklist:
        _blacklist.remove(chat_id)
        _save_blacklist()
    _save_whitelist()
    print(f"Gate Action: Whitelisted {chat_id}")

def blacklist_chat(chat_id: str):
    if not chat_id:
        return
    chat_id = str(chat_id).strip()
    _blacklist.add(chat_id)
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()
    _save_blacklist()
    print(f"Gate Action: Blacklisted {chat_id}")

def unwhitelist_chat(chat_id: str):
    chat_id = str(chat_id).strip()
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()
        print(f"Gate Action: Un-whitelisted {chat_id}")

def _save_whitelist():
    os.makedirs(os.path.dirname(WHITELIST_PATH), exist_ok=True)
    with open(WHITELIST_PATH, "w") as f:
        json.dump(list(_whitelist), f, indent=2)

def _save_blacklist():
    os.makedirs(os.path.dirname(BLACKLIST_PATH), exist_ok=True)
    with open(BLACKLIST_PATH, "w") as f:
        json.dump(list(_blacklist), f, indent=2)

def get_whitelist(): return list(_whitelist)
def get_blacklist(): return list(_blacklist)

def should_notify_admin(chat_id: str, cooldown: int = 3600) -> bool:
    if is_blacklisted(chat_id):
        return False
    now = time.time()
    last = _last_notified.get(chat_id, 0)
    if now - last > cooldown:
        _last_notified[chat_id] = now
        return True
    return False

# Bootstrapping
_load_data()
