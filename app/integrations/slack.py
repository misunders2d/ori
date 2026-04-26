"""Slack OAuth provider — Bot tokens via the v2 OAuth flow.

To use: register an OAuth app at https://api.slack.com/apps and set
    SLACK_OAUTH_CLIENT_ID
    SLACK_OAUTH_CLIENT_SECRET
    SLACK_OAUTH_REDIRECT_URI    # optional

Most installs use a workspace bot token directly via `SLACK_BOT_TOKEN`
(see app/util/config.py). This provider is for multi-workspace flows
where each workspace authenticates independently.

Slack's v2 OAuth response is non-standard (`access_token` lives at
`authed_user.access_token` for user tokens, or top-level `access_token`
for bot tokens — we read the bot token here). Slack does NOT issue
refresh tokens by default, so `refresh` is a no-op.
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


class SlackProvider(IntegrationProvider):
    name = "slack"
    default_scopes = ("chat:write", "channels:read", "users:read")
    _auth_url = "https://slack.com/oauth/v2/authorize"
    _token_url = "https://slack.com/api/oauth.v2.access"
    _revoke_url = "https://slack.com/api/auth.revoke"

    def auth_scheme(self) -> AuthScheme:
        return OAuth2(
            description="Slack OAuth 2.0 (v2)",
            flows=OAuthFlows(
                authorizationCode=OAuthFlowAuthorizationCode(
                    authorizationUrl=self._auth_url,
                    tokenUrl=self._token_url,
                    scopes={
                        "chat:write": "Send messages",
                        "channels:read": "List public channels",
                        "channels:history": "Read public channel messages",
                        "groups:read": "List private channels",
                        "im:read": "List DMs",
                        "users:read": "List users",
                        "files:write": "Upload files",
                    },
                ),
            ),
        )

    async def exchange_code(self, code: str) -> AuthCredential:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._token_url,
                data={
                    "code": code,
                    "client_id": self.client_id(),
                    "client_secret": self.client_secret(),
                    "redirect_uri": self.redirect_uri(),
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack OAuth error: {data.get('error', 'unknown')}")
        # Bot token first; fall back to user token if a bot token was not requested.
        access_token = data.get("access_token") or (
            (data.get("authed_user") or {}).get("access_token", "")
        )
        return AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(access_token=access_token),
        )

    async def refresh(self, refresh_token: str) -> AuthCredential:
        # Slack does not issue refresh tokens for the standard v2 flow.
        return AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(access_token=refresh_token),
        )
