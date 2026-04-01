import json
import logging
import os
import time

logger = logging.getLogger(__name__)

WHITELIST_PATH = os.path.abspath("./data/whitelist.json")
BLACKLIST_PATH = os.path.abspath("./data/blacklist.json")

# In-memory cache for speed
_whitelist = set()
_blacklist = set()
_last_notified = {}  # chat_id -> timestamp

def _load_data():
    global _whitelist, _blacklist
    # Combined set of all allowed IDs
    new_whitelist = set()
    new_blacklist = set()

    # 1. Load from Environment Variables
    env_allowed = os.environ.get("ALLOWED_USER_IDS", "").split(",")
    env_admins = os.environ.get("ADMIN_USER_IDS", "").split(",")
    
    for uid in (env_allowed + env_admins):
        clean_uid = uid.strip()
        if clean_uid:
            new_whitelist.add(clean_uid)
            logger.debug("Whitelist: Loaded %s from environment", clean_uid)

    # 2. Load from Whitelist JSON
    if os.path.exists(WHITELIST_PATH):
        try:
            with open(WHITELIST_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for uid in data:
                        if str(uid).strip():
                            new_whitelist.add(str(uid).strip())
                elif isinstance(data, dict):
                    for uid in data.keys():
                        if str(uid).strip():
                            new_whitelist.add(str(uid).strip())
        except Exception as e:
            logger.error("Failed to load whitelist from %s: %s", WHITELIST_PATH, e)

    # 3. Load from Blacklist JSON
    if os.path.exists(BLACKLIST_PATH):
        try:
            with open(BLACKLIST_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for uid in data:
                        new_blacklist.add(str(uid).strip())
                elif isinstance(data, dict):
                    for uid in data.keys():
                        new_blacklist.add(str(uid).strip())
        except Exception as e:
            logger.error("Failed to load blacklist from %s: %s", BLACKLIST_PATH, e)

    # Atomically update global sets
    _whitelist.clear()
    _whitelist.update(new_whitelist)
    _blacklist.clear()
    _blacklist.update(new_blacklist)

    if not _whitelist:
        logger.warning("Whitelist is COMPLETELY EMPTY. Access will be denied for ALL users including admins.")
    else:
        logger.info("Whitelist loaded. Total unique authorized IDs: %d", len(_whitelist))

def reload():
    """Manually trigger a reload of the whitelist and blacklist from disk/env."""
    _load_data()

def _save_whitelist():
    os.makedirs(os.path.dirname(WHITELIST_PATH), exist_ok=True)
    with open(WHITELIST_PATH, "w") as f:
        json.dump(list(_whitelist), f, indent=2)

def _save_blacklist():
    os.makedirs(os.path.dirname(BLACKLIST_PATH), exist_ok=True)
    with open(BLACKLIST_PATH, "w") as f:
        json.dump(list(_blacklist), f, indent=2)

# Initialize on import
_load_data()

def is_allowed(chat_id: str) -> bool:
    """Check if a chat/user ID is whitelisted. Strict fail-closed logic."""
    if not chat_id:
        return False
    
    chat_id_str = str(chat_id)
    
    if chat_id_str in _whitelist:
        logger.info("Gate: Access GRANTED for %s", chat_id_str)
        return True
    
    logger.warning("Gate: Access DENIED for %s (not in whitelist)", chat_id_str)
    return False

def is_blacklisted(chat_id: str) -> bool:
    """Check if a chat/user ID is explicitly blacklisted."""
    if not chat_id:
        return False
    return str(chat_id) in _blacklist

def whitelist_chat(chat_id: str):
    """Add a chat/user ID to the whitelist."""
    if not chat_id: return
    chat_id = str(chat_id).strip()
    _whitelist.add(chat_id)
    if chat_id in _blacklist:
        _blacklist.remove(chat_id)
        _save_blacklist()
    _save_whitelist()
    logger.info("Access Control: Whitelisted %s", chat_id)

def blacklist_chat(chat_id: str):
    """Add a chat/user ID to the blacklist (stops notifications)."""
    if not chat_id: return
    chat_id = str(chat_id).strip()
    _blacklist.add(chat_id)
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()
    _save_blacklist()
    logger.info("Access Control: Blacklisted %s", chat_id)

def unwhitelist_chat(chat_id: str):
    """Remove a chat/user ID from the whitelist."""
    chat_id = str(chat_id).strip()
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()
        logger.info("Access Control: Un-whitelisted %s", chat_id)

def get_whitelist():
    """Return the current whitelist."""
    return list(_whitelist)

def get_blacklist():
    """Return the current blacklist."""
    return list(_blacklist)

def should_notify_admin(chat_id: str, cooldown: int = 3600) -> bool:
    """Determine if we should notify the admin about an unauthorized attempt."""
    if is_blacklisted(chat_id):
        return False
    
    now = time.time()
    last = _last_notified.get(chat_id, 0)
    if now - last > cooldown:
        _last_notified[chat_id] = now
        return True
    return False
