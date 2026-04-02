import os
import sys
from app.app_utils.config import ALLOWED_CONFIG_KEYS, totp_enabled

print(f"PYTHONPATH: {sys.path}")
print(f"REQUIRE_2FA in ALLOWED_CONFIG_KEYS: {'REQUIRE_2FA' in ALLOWED_CONFIG_KEYS}")
print(f"REQUIRE_2FA in os.environ: {os.environ.get('REQUIRE_2FA')}")
print(f"ADMIN_TOTP_SECRET set: {bool(os.environ.get('ADMIN_TOTP_SECRET'))}")
print(f"totp_enabled(): {totp_enabled()}")
