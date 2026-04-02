import os
import pytest
import json
from unittest.mock import patch, MagicMock

from interfaces.setup_wizard import _decode_secret, _generate_code, verify_totp, main

def test_setup_wizard_totp_functions():
    """Verify that the native TOTP implementation ported to the setup wizard works correctly."""
    secret = "JBSWY3DPEHPK3PXP"
    time_step = 12345678
    code = _generate_code(secret, time_step)
    
    with patch('time.time', return_value=time_step * 30):
        assert verify_totp(secret, code) is True
        assert verify_totp(secret, "000000") is False

@patch("dotenv.set_key")
@patch("dotenv.load_dotenv")
@patch("builtins.input")
@patch("builtins.print")
@patch("interfaces.setup_wizard.clear_screen")
def test_setup_wizard_main_all_inputs(mock_clear, mock_print, mock_input, mock_load, mock_set_key, monkeypatch, tmp_path):
    """Test the complete interactive setup flow including TOTP enablement."""
    monkeypatch.setattr(os, "environ", {})
    
    env_file = tmp_path / ".env"
    
    # Mock Telegram API response
    mock_tg_response = MagicMock()
    mock_tg_response.read.return_value = json.dumps({
        "ok": True,
        "result": [{
            "update_id": 100,
            "message": {
                "from": {"id": 12345},
                "text": "ABCDEF"
            }
        }]
    }).encode()
    mock_tg_response.__enter__.return_value = mock_tg_response

    with patch("interfaces.setup_wizard.verify_totp", return_value=True):
        mock_input.side_effect = [
            "MyCustomBot",    # Bot Name
            "AIzaSyTestKey",  # Google Key
            "12345:ABCDE",    # Telegram Token
            "y",              # Configure GitHub?
            "user/my-bot",    # GitHub Repo
            "ghp_testToken",  # GitHub PAT
            "",               # Enter to continue Admin Passcode
            "y",              # Enable TOTP
            "123456",         # TOTP code
        ]
        
        with patch("interfaces.setup_wizard.os.path.abspath", return_value=str(env_file)):
            with patch("urllib.request.urlopen", return_value=mock_tg_response):
                with patch("secrets.token_hex", return_value="ABCDEF"):
                    main()
            
    # set_key should be called for: BOT_NAME, GOOGLE_API_KEY, TELEGRAM_BOT_TOKEN, ADMIN_USER_IDS, GITHUB_REPO, GITHUB_TOKEN, ADMIN_PASSCODE, A2A_API_KEY, ADMIN_TOTP_SECRET
    assert mock_set_key.call_count == 9

@patch("dotenv.set_key")
@patch("dotenv.load_dotenv")
@patch("builtins.input")
@patch("builtins.print")
@patch("interfaces.setup_wizard.clear_screen")
def test_setup_wizard_main_skip_optional(mock_clear, mock_print, mock_input, mock_load, mock_set_key, monkeypatch, tmp_path):
    """Test the interactive setup flow when optional components are skipped."""
    monkeypatch.setattr(os, "environ", {})
    
    env_file = tmp_path / ".env"
    
    mock_input.side_effect = [
        "",               # Skip Bot Name (defaults to Ori)
        "AIzaSyTestKey",  # Google Key
        "",               # Skip Telegram
        "n",              # Skip GitHub
        "",               # Enter to continue Admin Passcode
        "n",              # Skip TOTP
    ]
    
    with patch("interfaces.setup_wizard.os.path.abspath", return_value=str(env_file)):
        main()
        
    # set_key should be called for: BOT_NAME, GOOGLE_API_KEY, ADMIN_PASSCODE, A2A_API_KEY
    assert mock_set_key.call_count == 4
