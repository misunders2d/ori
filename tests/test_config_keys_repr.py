from app.app_utils.config import ALLOWED_CONFIG_KEYS
import pytest

def test_config_keys_repr():
    print(f"\nDEBUG_REPR: {repr(ALLOWED_CONFIG_KEYS)}")
    assert "REQUIRE_2FA" in ALLOWED_CONFIG_KEYS
