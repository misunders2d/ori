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
    # Load environment variables first
    env_allowed = os.environ.get("ALLOWED_USER_IDS", "").split(",")
    _whitelist = {u.strip() for u in env_allowed if u.strip()}

    if os.path.exists(WHITELIST_PATH):
        try:
            with open(WHITELIST_PATH, "r") as f:
                data = json.load(f)
                _whitelist.update(data)
        except Exception:
            logger.error("Failed to load whitelist from %s", WHITELIST_PATH)

    if os.path.exists(BLACKLIST_PATH):
        try:
            with open(BLACKLIST_PATH, "r") as f:
                data = json.load(f)
                _blacklist = set(data)
        except Exception:
            logger.error("Failed to load blacklist from %s", BLACKLIST_PATH)

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
    """Check if a chat/user ID is whitelisted."""
    return chat_id in _whitelist

def is_blacklisted(chat_id: str) -> bool:
    """Check if a chat/user ID is explicitly blacklisted."""
    return chat_id in _blacklist

def whitelist_chat(chat_id: str):
    """Add a chat/user ID to the whitelist."""
    _whitelist.add(chat_id)
    if chat_id in _blacklist:
        _blacklist.remove(chat_id)
        _save_blacklist()
    _save_whitelist()

def blacklist_chat(chat_id: str):
    """Add a chat/user ID to the blacklist (stops notifications)."""
    _blacklist.add(chat_id)
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()
    _save_blacklist()

def unwhitelist_chat(chat_id: str):
    """Remove a chat/user ID from the whitelist."""
    if chat_id in _whitelist:
        _whitelist.remove(chat_id)
        _save_whitelist()

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
