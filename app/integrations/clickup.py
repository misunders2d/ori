"""ClickUp OAuth provider.

To use: register an OAuth app at https://app.clickup.com/settings/team/<id>/apps
and set vault keys
    OAUTH_CLICKUP_CLIENT_ID
    OAUTH_CLICKUP_CLIENT_SECRET
    OAUTH_CLICKUP_REDIRECT_URI    # optional

ClickUp's OAuth flow is non-standard: token exchange uses query params
(not form-encoded body), and it issues a long-lived access token with no
refresh token. The base class POSTs form-encoded — overriding
`exchange_code` to match ClickUp's quirks.

Most ClickUp deployments skip OAuth entirely and use a personal API token
in `CLICKUP_API_TOKEN`. This provider is for multi-tenant installs where
each user authenticates separately.
"""

from __future__ import annotations

import httpx
from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows

from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_schemes import AuthScheme

from app.integrations.base import IntegrationProvider


class ClickUpProvider(IntegrationProvider):
    name = "clickup"
    default_scopes = ()  # ClickUp does not use scopes
    _auth_url = "https://app.clickup.com/api"
    _token_url = "https://api.clickup.com/api/v2/oauth/token"
    _revoke_url = ""  # no revoke endpoint

    def auth_scheme(self) -> AuthScheme:
        return OAuth2(
            description="ClickUp OAuth 2.0",
            flows=OAuthFlows(
                authorizationCode=OAuthFlowAuthorizationCode(
                    authorizationUrl=self._auth_url,
                    tokenUrl=self._token_url,
                    scopes={},
                ),
            ),
        )

    async def exchange_code(self, code: str) -> AuthCredential:
        # ClickUp wants params on the query string for the token POST.
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,
                params={
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(access_token=data.get("access_token", "")),
        )

    async def refresh(self, refresh_token: str) -> AuthCredential:
        # ClickUp tokens do not expire and there is no refresh flow.
        # Return the refresh_token as the access_token unchanged.
        return AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(access_token=refresh_token),
        )
