from app.app_utils.config import update_config, ALLOWED_CONFIG_KEYS
import os
import hmac

def test_init_logic():
    passcode = "testpass"
    # Ensure REQUIRE_2FA is in ALLOWED_CONFIG_KEYS
    print(f"DEBUG: ALLOWED_CONFIG_KEYS={ALLOWED_CONFIG_KEYS}")
    assert "REQUIRE_2FA" in ALLOWED_CONFIG_KEYS
    
    # Simulate /init call
    command = f"/init {passcode} REQUIRE_2FA=false"
    # We need to mock totp_enabled to False to avoid TOTP prompt
    import app.app_utils.config as config_mod
    config_mod.totp_enabled = lambda: False
    
    result = update_config(command, passcode, "test_session")
    print(f"DEBUG: result={result}")
    assert "Configuration updated for: REQUIRE_2FA" in result
    assert "Rejected" not in result
