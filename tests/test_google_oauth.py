import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.tools.google_oauth.token_store import save_token, get_token, delete_token, list_connected_users


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_path = str(tmp_path / "test_tokens.db")
    with patch("app.tools.google_oauth.token_store._DB_PATH", db_path):
        yield


def test_save_and_get_token():
    save_token("user@test.com", "access123", "refresh456", 3600, ["drive", "sheets"])
    token = get_token("user@test.com")
    assert token is not None
    assert token["access_token"] == "access123"
    assert token["refresh_token"] == "refresh456"
    assert token["expired"] is False


def test_get_nonexistent_token():
    assert get_token("nobody@test.com") is None


def test_delete_token():
    save_token("user@test.com", "a", "r", 3600, ["drive"])
    delete_token("user@test.com")
    assert get_token("user@test.com") is None


def test_list_connected_users():
    save_token("a@test.com", "a", "r", 3600, ["drive"])
    save_token("b@test.com", "a", "r", 3600, ["drive"])
    users = list_connected_users()
    assert "a@test.com" in users
    assert "b@test.com" in users


def test_overwrite_token():
    save_token("user@test.com", "old", "old_r", 3600, ["drive"])
    save_token("user@test.com", "new", "new_r", 7200, ["drive", "sheets"])
    token = get_token("user@test.com")
    assert token["access_token"] == "new"
    assert token["refresh_token"] == "new_r"


def test_expired_token():
    save_token("user@test.com", "a", "r", -1, ["drive"])  # Already expired
    token = get_token("user@test.com")
    assert token["expired"] is True


@pytest.mark.asyncio
async def test_google_connect_no_client_id():
    from app.tools.google_drive import google_connect
    mock_ctx = MagicMock()
    mock_ctx.state.to_dict.return_value = {"user_id": "user@test.com"}
    with patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": ""}):
        result = await google_connect(tool_context=mock_ctx)
        assert result["status"] == "error"
        assert "CLIENT_ID" in result["message"]


@pytest.mark.asyncio
async def test_drive_list_not_connected():
    from app.tools.google_drive import drive_list_files
    mock_ctx = MagicMock()
    mock_ctx.state.to_dict.return_value = {"user_id": "nobody@test.com"}
    with patch("app.tools.google_drive.get_token", return_value=None):
        result = await drive_list_files(tool_context=mock_ctx)
        assert result["status"] == "error"
        assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_sheets_read_not_connected():
    from app.tools.google_drive import sheets_read
    mock_ctx = MagicMock()
    mock_ctx.state.to_dict.return_value = {"user_id": "nobody@test.com"}
    with patch("app.tools.google_drive.get_token", return_value=None):
        result = await sheets_read("fake_id", tool_context=mock_ctx)
        assert result["status"] == "error"
        assert "not connected" in result["message"].lower()
