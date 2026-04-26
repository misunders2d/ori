"""IntegrationProvider — the contract every OAuth integration implements.

A provider declares:
- name: short identifier (e.g. "google", "github") — used as the registry
  key and as the OAuth scheme name namespacing in the credential service.
- scopes: default scopes requested by `authorize_url`. Tools requesting
  fewer/more scopes can override per-call.
- auth_scheme: an ADK `AuthScheme` (typically `OAuth2`) that ADK's runtime
  attaches to AuthConfig when calling load_credential / save_credential.

And implements:
- async authorize_url(state) -> str — build the user-facing redirect.
- async exchange_code(code) -> AuthCredential — POST to the token endpoint.
- async refresh(refresh_token) -> AuthCredential — refresh an expired token.
- async revoke(access_token) -> None — server-side token revocation.
- async scope_check(token, required) -> bool — does the token cover scopes?

Concrete providers (google.py, github.py) self-register on import in
app/integrations/__init__.py.

Provider config (client_id, client_secret) is pulled from the vault at
provider construction. To add credentials for a provider, set vault keys:
    <PROVIDER_UPPER>_OAUTH_CLIENT_ID
    <PROVIDER_UPPER>_OAUTH_CLIENT_SECRET
    <PROVIDER_UPPER>_OAUTH_REDIRECT_URI    # optional; default uses A2A_BASE_URL
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlencode

import httpx
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_schemes import AuthScheme

from deploy import vault

logger = logging.getLogger(__name__)


def _vault_lookup(provider: str, suffix: str, default: str = "") -> str:
    """Read a per-provider vault key. Falls back to env, then default.

    Convention: `<PROVIDER>_OAUTH_<SUFFIX>` (e.g. `GOOGLE_OAUTH_CLIENT_ID`).
    Provider-prefixed to match the rest of the project's env naming and
    to share keys with legacy per-user OAuth tooling.
    """
    key = f"{provider.upper()}_OAUTH_{suffix.upper()}"
    return vault.get(key) or os.environ.get(key) or default


class IntegrationProvider(ABC):
    """Abstract base for OAuth providers.

    Subclasses set class attributes `name`, `default_scopes`,
    `_auth_url`, `_token_url`, optionally `_revoke_url`, and override
    `auth_scheme` to return the right ADK AuthScheme for their flow.
    """

    name: ClassVar[str] = ""
    default_scopes: ClassVar[tuple[str, ...]] = ()
    _auth_url: ClassVar[str] = ""
    _token_url: ClassVar[str] = ""
    _revoke_url: ClassVar[str] = ""

    # ------- credentials (lazy from vault) ----------------------------------

    def client_id(self) -> str:
        return _vault_lookup(self.name, "CLIENT_ID")

    def client_secret(self) -> str:
        return _vault_lookup(self.name, "CLIENT_SECRET")

    def redirect_uri(self) -> str:
        explicit = _vault_lookup(self.name, "REDIRECT_URI")
        if explicit:
            return explicit
        # Default callback path on the A2A server.
        base = os.environ.get("A2A_BASE_URL", "http://localhost:8000").rstrip("/")
        return f"{base}/oauth/{self.name}/callback"

    # ------- ADK AuthScheme -------------------------------------------------

    @abstractmethod
    def auth_scheme(self) -> AuthScheme:
        """Return the ADK AuthScheme describing this provider's OAuth flow."""

    # ------- OAuth flow -----------------------------------------------------

    async def authorize_url(self, state: str, scopes: list[str] | None = None) -> str:
        """Build the user-facing authorization URL."""
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "scope": " ".join(scopes or self.default_scopes),
            "state": state,
            "access_type": "offline",  # request refresh token where supported
        }
        return f"{self._auth_url}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> AuthCredential:
        """Exchange an authorization code for tokens."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri(),
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return _credential_from_token_response(data)

    async def refresh(self, refresh_token: str) -> AuthCredential:
        """Refresh an expired access token."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        # Some providers don't echo refresh_token on refresh; preserve the old.
        if not data.get("refresh_token"):
            data["refresh_token"] = refresh_token
        return _credential_from_token_response(data)

    async def revoke(self, access_token: str) -> None:
        """Revoke an access token. No-op if provider has no revoke endpoint."""
        if not self._revoke_url:
            logger.info("IntegrationProvider %s: no revoke endpoint", self.name)
            return
        async with httpx.AsyncClient() as client:
            await client.post(self._revoke_url, data={"token": access_token})

    async def scope_check(self, token: str, required: list[str]) -> bool:
        """Default: trust the token (most providers don't expose scope-check).
        Subclasses can override to introspect when supported.
        """
        return True


def _credential_from_token_response(data: dict) -> AuthCredential:
    """Wrap a token response in an ADK AuthCredential."""
    return AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token") or None,
        ),
    )
