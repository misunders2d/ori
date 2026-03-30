"""Tests for the universal OAuth2 service (platform registry, tokens, PKCE)."""

import json
import os
import time
import pytest

from app.core.auth import OAuthService


@pytest.fixture
def tmp_service(tmp_path):
    """Create an OAuthService with temp file paths."""
    platforms_path = str(tmp_path / "platforms.json")
    tokens_path = str(tmp_path / "tokens.json")

    # Patch module-level paths
    import app.core.auth as auth_mod
    orig_platforms = auth_mod.PLATFORMS_PATH
    orig_tokens = auth_mod.TOKENS_PATH
    auth_mod.PLATFORMS_PATH = platforms_path
    auth_mod.TOKENS_PATH = tokens_path

    svc = OAuthService()
    yield svc

    auth_mod.PLATFORMS_PATH = orig_platforms
    auth_mod.TOKENS_PATH = orig_tokens


# -----------------------------------------------------------------------
# Platform Registry
# -----------------------------------------------------------------------

def test_register_device_code_platform(tmp_service):
    tmp_service.register_platform("google", {
        "name": "Google",
        "flow": "device_code",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "device_code_endpoint": "https://oauth2.googleapis.com/device/code",
        "client_id": "test-id",
        "client_secret": "test-secret",
        "default_scopes": ["openid", "email"],
    })
    p = tmp_service.get_platform("google")
    assert p is not None
    assert p["name"] == "Google"
    assert p["flow"] == "device_code"


def test_register_auth_code_pkce_platform(tmp_service):
    tmp_service.register_platform("dropbox", {
        "name": "Dropbox",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://api.dropboxapi.com/oauth2/token",
        "auth_endpoint": "https://www.dropbox.com/oauth2/authorize",
        "client_id": "test-id",
        "client_secret": "",
        "default_scopes": [],
    })
    p = tmp_service.get_platform("dropbox")
    assert p is not None
    assert p["flow"] == "auth_code_pkce"


def test_register_rejects_missing_fields(tmp_service):
    with pytest.raises(ValueError, match="Missing required fields"):
        tmp_service.register_platform("bad", {"name": "Bad"})


def test_register_rejects_invalid_flow(tmp_service):
    with pytest.raises(ValueError, match="Unsupported flow"):
        tmp_service.register_platform("bad", {
            "name": "Bad",
            "flow": "magic",
            "token_endpoint": "https://example.com/token",
        })


def test_register_device_code_requires_endpoint(tmp_service):
    with pytest.raises(ValueError, match="device_code_endpoint"):
        tmp_service.register_platform("bad", {
            "name": "Bad",
            "flow": "device_code",
            "token_endpoint": "https://example.com/token",
        })


def test_register_auth_code_requires_auth_endpoint(tmp_service):
    with pytest.raises(ValueError, match="auth_endpoint"):
        tmp_service.register_platform("bad", {
            "name": "Bad",
            "flow": "auth_code_pkce",
            "token_endpoint": "https://example.com/token",
        })


def test_remove_platform(tmp_service):
    tmp_service.register_platform("test", {
        "name": "Test",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://example.com/token",
        "auth_endpoint": "https://example.com/auth",
    })
    assert tmp_service.get_platform("test") is not None
    tmp_service.remove_platform("test")
    assert tmp_service.get_platform("test") is None


def test_remove_nonexistent_platform(tmp_service):
    """Removing a platform that doesn't exist should not raise."""
    tmp_service.remove_platform("ghost")


def test_list_platforms_empty(tmp_service):
    assert tmp_service.list_platforms() == {}


def test_list_platforms_with_entries(tmp_service):
    tmp_service.register_platform("svc", {
        "name": "Service",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://example.com/token",
        "auth_endpoint": "https://example.com/auth",
    })
    result = tmp_service.list_platforms()
    assert "svc" in result
    assert result["svc"]["name"] == "Service"
    assert result["svc"]["connected"] is False


def test_platform_persistence(tmp_service):
    """Platforms should survive a service restart (reload from file)."""
    import app.core.auth as auth_mod
    tmp_service.register_platform("persist", {
        "name": "Persistent",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://example.com/token",
        "auth_endpoint": "https://example.com/auth",
    })
    svc2 = OAuthService()
    assert svc2.get_platform("persist") is not None
    assert svc2.get_platform("persist")["name"] == "Persistent"


# -----------------------------------------------------------------------
# Token Management
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_token_returns_none_when_empty(tmp_service):
    assert await tmp_service.get_token("nonexistent") is None


@pytest.mark.asyncio
async def test_get_token_returns_valid_token(tmp_service):
    tmp_service._tokens["test"] = {
        "access_token": "valid-token",
        "refresh_token": None,
        "expires_at": time.time() + 3600,
        "scopes": [],
    }
    assert await tmp_service.get_token("test") == "valid-token"


@pytest.mark.asyncio
async def test_get_token_returns_none_when_expired_no_refresh(tmp_service):
    tmp_service._tokens["test"] = {
        "access_token": "expired-token",
        "refresh_token": None,
        "expires_at": time.time() - 100,
        "scopes": [],
    }
    assert await tmp_service.get_token("test") is None


def test_disconnect_removes_tokens(tmp_service):
    tmp_service._tokens["test"] = {"access_token": "tok", "expires_at": time.time() + 3600}
    tmp_service.disconnect("test")
    assert "test" not in tmp_service._tokens


def test_store_token(tmp_service):
    tmp_service._store_token("svc", {
        "access_token": "abc123",
        "refresh_token": "ref456",
        "expires_in": 7200,
        "scope": "read write",
    })
    stored = tmp_service._tokens["svc"]
    assert stored["access_token"] == "abc123"
    assert stored["refresh_token"] == "ref456"
    assert stored["expires_at"] > time.time()
    assert "read" in stored["scopes"]
    assert "write" in stored["scopes"]


# -----------------------------------------------------------------------
# PKCE
# -----------------------------------------------------------------------

def test_pkce_generation():
    v, c = OAuthService._generate_pkce()
    assert len(v) > 20
    assert len(c) > 20
    assert v != c


def test_pkce_determinism():
    """Two calls should produce different verifiers (random)."""
    v1, _ = OAuthService._generate_pkce()
    v2, _ = OAuthService._generate_pkce()
    assert v1 != v2


def test_auth_code_flow_generates_url(tmp_service):
    tmp_service.register_platform("dropbox", {
        "name": "Dropbox",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://api.dropboxapi.com/oauth2/token",
        "auth_endpoint": "https://www.dropbox.com/oauth2/authorize",
        "client_id": "test-client-id",
        "client_secret": "",
        "default_scopes": ["files.content.read"],
    })
    url = tmp_service.start_auth_code_flow("dropbox")
    assert url.startswith("https://www.dropbox.com/oauth2/authorize?")
    assert "client_id=test-client-id" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "files.content.read" in url
    assert tmp_service.has_pending_auth_code("dropbox") is True


def test_auth_code_flow_wrong_platform_type(tmp_service):
    tmp_service.register_platform("google", {
        "name": "Google",
        "flow": "device_code",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "device_code_endpoint": "https://oauth2.googleapis.com/device/code",
    })
    with pytest.raises(ValueError, match="not 'auth_code_pkce'"):
        tmp_service.start_auth_code_flow("google")


def test_auth_code_flow_unregistered(tmp_service):
    with pytest.raises(ValueError, match="not registered"):
        tmp_service.start_auth_code_flow("ghost")


def test_has_pending_auth_code_false(tmp_service):
    assert tmp_service.has_pending_auth_code("ghost") is False


def test_extra_auth_params(tmp_service):
    """Extra auth params should be included in the authorization URL."""
    tmp_service.register_platform("google_cal", {
        "name": "Google Calendar",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "auth_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "client_id": "test",
        "client_secret": "secret",
        "default_scopes": ["https://www.googleapis.com/auth/calendar"],
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    })
    url = tmp_service.start_auth_code_flow("google_cal")
    assert "access_type=offline" in url
    assert "prompt=consent" in url


# -----------------------------------------------------------------------
# List platforms with token status
# -----------------------------------------------------------------------

def test_list_platforms_shows_connected(tmp_service):
    tmp_service.register_platform("svc", {
        "name": "Service",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://example.com/token",
        "auth_endpoint": "https://example.com/auth",
    })
    tmp_service._tokens["svc"] = {
        "access_token": "tok",
        "expires_at": time.time() + 3600,
    }
    result = tmp_service.list_platforms()
    assert result["svc"]["connected"] is True


def test_list_platforms_shows_expired(tmp_service):
    tmp_service.register_platform("svc", {
        "name": "Service",
        "flow": "auth_code_pkce",
        "token_endpoint": "https://example.com/token",
        "auth_endpoint": "https://example.com/auth",
    })
    tmp_service._tokens["svc"] = {
        "access_token": "tok",
        "expires_at": time.time() - 100,
    }
    result = tmp_service.list_platforms()
    assert result["svc"]["connected"] is False
