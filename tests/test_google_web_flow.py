"""Tests for the Google OAuth2 Authorization Code + PKCE flow (replaces device flow)."""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.tools.google_oauth import web_flow


@pytest.fixture(autouse=True)
def clean_state():
    """Clear pending-flow state and pin env vars for each test."""
    web_flow._PENDING.clear()
    env = {
        "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
        "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
        "OAUTH_BASE_URL": "https://bezosapp.uk",
    }
    with patch.dict(os.environ, env, clear=False):
        yield
    web_flow._PENDING.clear()


def test_start_auth_flow_returns_url_with_pkce_params():
    result = web_flow.start_auth_flow("user@test.com")
    assert result["status"] == "success"
    assert "auth_url" in result
    assert "state" in result

    parsed = urlparse(result["auth_url"])
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"

    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["test-client-id"]
    assert qs["response_type"] == ["code"]
    assert qs["redirect_uri"] == ["https://bezosapp.uk/oauth/google/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["access_type"] == ["offline"]
    assert "gmail.readonly" in qs["scope"][0]
    assert "drive.file" in qs["scope"][0]
    assert qs["state"] == [result["state"]]
    # PKCE challenge is present and non-empty
    assert qs["code_challenge"][0]


def test_start_auth_flow_stores_pending_state():
    result = web_flow.start_auth_flow("user@test.com")
    state = result["state"]
    assert state in web_flow._PENDING
    assert web_flow._PENDING[state]["user_id"] == "user@test.com"
    assert web_flow._PENDING[state]["code_verifier"]


def test_start_auth_flow_no_client_id():
    with patch.dict(os.environ, {"GOOGLE_OAUTH_CLIENT_ID": ""}):
        result = web_flow.start_auth_flow("user@test.com")
    assert result["status"] == "error"
    assert "CLIENT_ID" in result["message"]


def test_start_auth_flow_no_base_url():
    with patch.dict(os.environ, {"OAUTH_BASE_URL": ""}):
        result = web_flow.start_auth_flow("user@test.com")
    assert result["status"] == "error"
    assert "OAUTH_BASE_URL" in result["message"]


def test_sweep_pending_removes_expired():
    web_flow._PENDING["fresh"] = {"user_id": "a", "code_verifier": "v", "created_at": time.time()}
    web_flow._PENDING["old"] = {"user_id": "b", "code_verifier": "v", "created_at": time.time() - 9999}
    web_flow._sweep_pending()
    assert "fresh" in web_flow._PENDING
    assert "old" not in web_flow._PENDING


@pytest.mark.asyncio
async def test_exchange_code_invalid_state():
    result = await web_flow.exchange_code("unknown-state", "some-code")
    assert result["status"] == "error"
    assert "state" in result["message"].lower()


@pytest.mark.asyncio
async def test_exchange_code_expired_state():
    state = "expired-state"
    web_flow._PENDING[state] = {
        "user_id": "user@test.com",
        "code_verifier": "v",
        "created_at": time.time() - 9999,
    }
    result = await web_flow.exchange_code(state, "code")
    assert result["status"] == "error"
    assert "expired" in result["message"].lower() or "state" in result["message"].lower()


@pytest.mark.asyncio
async def test_exchange_code_happy_path():
    # Seed pending state
    state = "happy-state"
    web_flow._PENDING[state] = {
        "user_id": "user@test.com",
        "code_verifier": "verifier-123",
        "created_at": time.time(),
    }

    # Mock httpx: first POST returns tokens, then GET returns email
    token_resp = MagicMock()
    token_resp.json.return_value = {
        "access_token": "access-xyz",
        "refresh_token": "refresh-abc",
        "expires_in": 3600,
    }
    email_resp = MagicMock()
    email_resp.json.return_value = {"email": "user@gmail.com"}

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=token_resp)
    client.get = AsyncMock(return_value=email_resp)
    client_cls = MagicMock(return_value=client)

    with patch("app.tools.google_oauth.web_flow.httpx.AsyncClient", client_cls):
        result = await web_flow.exchange_code(state, "auth-code")

    assert result["status"] == "success"
    assert result["user_id"] == "user@test.com"
    assert result["access_token"] == "access-xyz"
    assert result["refresh_token"] == "refresh-abc"
    assert result["expires_in"] == 3600
    assert result["email"] == "user@gmail.com"
    # State is consumed
    assert state not in web_flow._PENDING


@pytest.mark.asyncio
async def test_oauth_callback_handler_happy_path():
    """The a2a_server callback handler persists tokens and renders a success page."""
    from app.a2a_server import _handle_oauth_callback

    # Mock request with state/code query params
    req = MagicMock()
    req.query_params = {"state": "s1", "code": "c1", "error": ""}

    with patch("app.tools.google_oauth.web_flow.exchange_code", AsyncMock(return_value={
        "status": "success",
        "user_id": "tg_330959414",
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": 3600,
        "email": "user@gmail.com",
    })), patch("app.tools.google_oauth.token_store.save_token") as save_token, \
         patch("app.tools.google_oauth.token_store.save_user_mapping") as save_mapping:
        resp = await _handle_oauth_callback(req)

    assert resp.status_code == 200
    save_token.assert_called_once()
    args, _kwargs = save_token.call_args
    assert args[0] == "user@gmail.com"  # email keys the token store
    save_mapping.assert_called_once_with("tg_330959414", "user@gmail.com")
    body = resp.body.decode()
    assert "Connected" in body
    assert "user@gmail.com" in body


@pytest.mark.asyncio
async def test_oauth_callback_handler_denied():
    from app.a2a_server import _handle_oauth_callback

    req = MagicMock()
    req.query_params = {"error": "access_denied", "state": "s", "code": ""}

    resp = await _handle_oauth_callback(req)
    assert resp.status_code == 400
    assert "denied" in resp.body.decode().lower()


@pytest.mark.asyncio
async def test_oauth_callback_handler_exchange_fails():
    from app.a2a_server import _handle_oauth_callback

    req = MagicMock()
    req.query_params = {"state": "bad", "code": "c", "error": ""}

    with patch("app.tools.google_oauth.web_flow.exchange_code", AsyncMock(return_value={
        "status": "error",
        "message": "Invalid or unknown state parameter.",
    })):
        resp = await _handle_oauth_callback(req)

    assert resp.status_code == 400
    assert "Invalid or unknown state" in resp.body.decode()


@pytest.mark.asyncio
async def test_exchange_code_google_error():
    state = "error-state"
    web_flow._PENDING[state] = {
        "user_id": "user@test.com",
        "code_verifier": "v",
        "created_at": time.time(),
    }

    err_resp = MagicMock()
    err_resp.json.return_value = {
        "error": "invalid_grant",
        "error_description": "Bad code",
    }

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = AsyncMock(return_value=err_resp)
    client_cls = MagicMock(return_value=client)

    with patch("app.tools.google_oauth.web_flow.httpx.AsyncClient", client_cls):
        result = await web_flow.exchange_code(state, "bad-code")

    assert result["status"] == "error"
    assert "Bad code" in result["message"]
    # State consumed even on error (can't retry with same state)
    assert state not in web_flow._PENDING
