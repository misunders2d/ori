import os
from app.core.whitelist import get_whitelist, get_blacklist

print(f"ALLOWED_USER_IDS (env): {os.environ.get('ALLOWED_USER_IDS')}")
print(f"ADMIN_USER_IDS (env): {os.environ.get('ADMIN_USER_IDS')}")
print(f"Whitelist cache: {get_whitelist()}")
print(f"Blacklist cache: {get_blacklist()}")
