import os
from app.core.whitelist import _whitelist, get_whitelist

print(f"Whitelist cache size: {len(_whitelist)}")
print(f"Whitelist content: {get_whitelist()}")
print(f"ALLOWED_USER_IDS env: {os.environ.get('ALLOWED_USER_IDS')}")
print(f"ADMIN_USER_IDS env: {os.environ.get('ADMIN_USER_IDS')}")
