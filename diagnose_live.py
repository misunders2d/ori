from app.app_utils.config import ALLOWED_CONFIG_KEYS
import os
print(f"ALLOWED_CONFIG_KEYS: {list(ALLOWED_CONFIG_KEYS)}")
print(f"REQUIRE_2FA in ALLOWED_CONFIG_KEYS: {'REQUIRE_2FA' in ALLOWED_CONFIG_KEYS}")
print(f"File path: {os.path.abspath('app/app_utils/config.py')}")
with open('app/app_utils/config.py', 'r') as f:
    content = f.read()
    print("Content preview:")
    print(content[:500])
