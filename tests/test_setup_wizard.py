import json
import os
from unittest.mock import MagicMock, patch

from app.transports.setup_wizard import (
    _generate_code,
    main,
    verify_totp,
)


def test_setup_wizard_totp_functions():
    """Verify that the native TOTP implementation works correctly."""
    secret = "JBSWY3DPEHPK3PXP"
    time_step = 12345678
    code = _generate_code(secret, time_step)

    with patch('time.time', return_value=time_step * 30):
        assert verify_totp(secret, code) is True
        assert verify_totp(secret, "000000") is False


@patch("builtins.input")
@patch("builtins.print")
@patch("app.transports.setup_wizard.clear_screen")
def test_setup_wizard_main_all_inputs(mock_clear, mock_print, mock_input, monkeypatch, tmp_path):
    """Test the complete interactive setup flow including TOTP enablement."""
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)

    # Create data dir structure
    data_dir = tmp_path / "data"
    data_dir.mkdir()

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
    mock_tg_response.__enter__ = lambda s: s
    mock_tg_response.__exit__ = MagicMock(return_value=False)

    with patch("app.transports.setup_wizard.verify_totp", return_value=True):
        mock_input.side_effect = [
            "MyCustomBot",    # Bot Name
            "1",              # Provider: Google API key
            "AIzaSyTestKey",  # Google Key
            "1",              # Model selection
            "12345:ABCDE",    # Telegram Token
            "y",              # Configure GitHub?
            "user/my-bot",    # GitHub Repo
            "ghp_testToken",  # GitHub PAT
            "",               # Enter to continue Admin Passcode
            "y",              # Enable TOTP
            "123456",         # TOTP code
        ]

        with patch("urllib.request.urlopen", return_value=mock_tg_response):
            with patch("secrets.token_hex", return_value="ABCDEF"):
                main()

    # Verify vault was created with all expected keys
    vault_file = data_dir / "vault" / "credentials.json"
    assert vault_file.exists(), "Vault file should be created"
    vault_data = json.loads(vault_file.read_text())
    assert vault_data["BOT_NAME"] == "MyCustomBot"
    assert vault_data["GOOGLE_API_KEY"] == "AIzaSyTestKey"
    assert "ADMIN_PASSCODE" in vault_data
    assert "A2A_API_KEY" in vault_data
    assert "TELEGRAM_BOT_TOKEN" in vault_data
    assert "GITHUB_REPO" in vault_data
    assert "ADMIN_TOTP_SECRET" in vault_data


@patch("builtins.input")
@patch("builtins.print")
@patch("app.transports.setup_wizard.clear_screen")
def test_setup_wizard_main_skip_optional(mock_clear, mock_print, mock_input, monkeypatch, tmp_path):
    """Test the setup flow when optional components are skipped."""
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.chdir(tmp_path)

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    mock_input.side_effect = [
        "",               # Skip Bot Name (defaults to Ori)
        "1",              # Provider: Google API key
        "AIzaSyTestKey",  # Google Key
        "1",              # Model selection
        "",               # Skip Telegram
        "n",              # Skip GitHub
        "",               # Enter to continue Admin Passcode
        "n",              # Skip TOTP
    ]

    main()

    vault_file = data_dir / "vault" / "credentials.json"
    assert vault_file.exists(), "Vault file should be created"
    vault_data = json.loads(vault_file.read_text())
    assert vault_data["GOOGLE_API_KEY"] == "AIzaSyTestKey"
    assert "ADMIN_PASSCODE" in vault_data
    assert "A2A_API_KEY" in vault_data
    # Optional keys should NOT be present
    assert vault_data.get("TELEGRAM_BOT_TOKEN", "") == ""
    assert "GITHUB_REPO" not in vault_data or vault_data["GITHUB_REPO"] == ""
