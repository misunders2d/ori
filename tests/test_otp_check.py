import os
import sys
from app.app_utils.config import ALLOWED_CONFIG_KEYS, totp_enabled
import pytest

def test_otp_check():
    print(f"\nDEBUG_OTP: REQUIRE_2FA in ALLOWED_CONFIG_KEYS: {'REQUIRE_2FA' in ALLOWED_CONFIG_KEYS}")
    print(f"DEBUG_OTP: REQUIRE_2FA in os.environ: {os.environ.get('REQUIRE_2FA')}")
    print(f"DEBUG_OTP: totp_enabled(): {totp_enabled()}")
    assert "REQUIRE_2FA" in ALLOWED_CONFIG_KEYS
