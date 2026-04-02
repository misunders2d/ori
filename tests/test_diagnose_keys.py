from app.app_utils.config import ALLOWED_CONFIG_KEYS
import pytest

def test_diagnose_keys():
    print(f"\nDIAGNOSE: {list(ALLOWED_CONFIG_KEYS)}")
    assert "REQUIRE_2FA" in ALLOWED_CONFIG_KEYS
