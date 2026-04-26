"""OAuth integrations subsystem — registry, base flow, mocked HTTP."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.openapi.models import OAuth2
from google.adk.auth.auth_credential import AuthCredentialTypes

from app.integrations import (
    REGISTRY,
    GitHubProvider,
    GoogleProvider,
    IntegrationProvider,
    get_provider,
    list_providers,
    register_provider,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_contains_bundled_providers():
    assert "google" in REGISTRY
    assert "github" in REGISTRY
    assert isinstance(REGISTRY["google"], GoogleProvider)
    assert isinstance(REGISTRY["github"], GitHubProvider)


def test_list_providers_sorted():
    assert list_providers() == sorted(list_providers())


def test_register_replaces_existing():
    """Later registrations replace earlier — supports test/dev override."""
    class FakeProvider(IntegrationProvider):
        name = "google"
        def auth_scheme(self):
            return MagicMock()

    original = REGISTRY["google"]
    fake = FakeProvider()
    try:
        register_provider(fake)
        assert get_provider("google") is fake
    finally:
        register_provider(original)
        assert get_provider("google") is original


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        get_provider("nonexistent-provider-xyz")


# ---------------------------------------------------------------------------
# Provider configuration via vault
# ---------------------------------------------------------------------------

def test_google_provider_reads_vault(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": {
            "OAUTH_GOOGLE_CLIENT_ID": "abc-id",
            "OAUTH_GOOGLE_CLIENT_SECRET": "abc-secret",
        }.get(key, default),
    )
    g = GoogleProvider()
    assert g.client_id() == "abc-id"
    assert g.client_secret() == "abc-secret"


def test_redirect_uri_default_uses_a2a_base_url(monkeypatch):
    monkeypatch.setattr("app.integrations.base.vault.get", lambda key, default="": "")
    monkeypatch.setenv("A2A_BASE_URL", "https://ori.example.com")
    monkeypatch.delenv("OAUTH_GOOGLE_REDIRECT_URI", raising=False)
    g = GoogleProvider()
    assert g.redirect_uri() == "https://ori.example.com/oauth/google/callback"


def test_redirect_uri_explicit_overrides(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": "https://my.callback/oauth"
        if key == "OAUTH_GOOGLE_REDIRECT_URI"
        else "",
    )
    g = GoogleProvider()
    assert g.redirect_uri() == "https://my.callback/oauth"


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authorize_url_includes_state_and_scopes(monkeypatch):
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": "client-abc" if "CLIENT_ID" in key else "",
    )
    monkeypatch.setenv("A2A_BASE_URL", "https://ori.example.com")
    g = GoogleProvider()
    url = await g.authorize_url(state="opaque-state", scopes=["openid", "https://www.googleapis.com/auth/drive.readonly"])
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=client-abc" in url
    assert "state=opaque-state" in url
    assert "openid" in url
    assert "drive.readonly" in url
    assert "redirect_uri=" in url


@pytest.mark.asyncio
async def test_exchange_code_returns_oauth2_credential(monkeypatch):
    """Mock httpx; verify exchange_code returns a properly-typed AuthCredential."""
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": "x" if "CLIENT" in key else "",
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "access_token": "tok-abc",
        "refresh_token": "ref-xyz",
        "token_type": "Bearer",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    with patch("app.integrations.base.httpx.AsyncClient", return_value=mock_client):
        g = GoogleProvider()
        cred = await g.exchange_code("auth-code-123")
    assert cred.auth_type == AuthCredentialTypes.OAUTH2
    assert cred.oauth2.access_token == "tok-abc"
    assert cred.oauth2.refresh_token == "ref-xyz"


@pytest.mark.asyncio
async def test_refresh_preserves_refresh_token_when_omitted(monkeypatch):
    """Some providers return only access_token on refresh; preserve the existing refresh_token."""
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": "x" if "CLIENT" in key else "",
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "new-tok"}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()

    with patch("app.integrations.base.httpx.AsyncClient", return_value=mock_client):
        g = GoogleProvider()
        cred = await g.refresh("original-refresh")
    assert cred.oauth2.access_token == "new-tok"
    assert cred.oauth2.refresh_token == "original-refresh"


@pytest.mark.asyncio
async def test_revoke_skipped_when_no_endpoint():
    """GitHub provider has no revoke URL — should be a no-op without HTTP call."""
    gh = GitHubProvider()
    # No exception, no HTTP call (httpx is sandboxed by conftest).
    await gh.revoke("any-token")


# ---------------------------------------------------------------------------
# Auth schemes
# ---------------------------------------------------------------------------

def test_google_auth_scheme_is_oauth2_with_authcode_flow():
    scheme = GoogleProvider().auth_scheme()
    assert isinstance(scheme, OAuth2)
    assert scheme.flows.authorizationCode is not None
    assert "https://www.googleapis.com/auth/drive.readonly" in scheme.flows.authorizationCode.scopes


def test_github_auth_scheme_is_oauth2():
    scheme = GitHubProvider().auth_scheme()
    assert isinstance(scheme, OAuth2)
    assert "repo" in scheme.flows.authorizationCode.scopes
