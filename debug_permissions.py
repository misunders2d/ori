import os
import json
from app.core.whitelist import _whitelist, _blacklist, is_allowed

print(f"ALLOWED_USER_IDS: {os.environ.get('ALLOWED_USER_IDS')}")
print(f"Whitelist cache: {_whitelist}")
print(f"Blacklist cache: {_blacklist}")

test_id = "tg_330959414"
print(f"Is {test_id} allowed? {is_allowed(test_id)}")
