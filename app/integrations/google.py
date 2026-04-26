"""Google OAuth provider — Drive, Gmail, Calendar, etc.

To use: set vault keys
    OAUTH_GOOGLE_CLIENT_ID
    OAUTH_GOOGLE_CLIENT_SECRET
    OAUTH_GOOGLE_REDIRECT_URI    # optional

Default scopes are scoped to "openid email" — enough for identity. Tools
that need more (Drive, Gmail) request additional scopes per-call.
"""

from __future__ import annotations

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.auth.auth_schemes import AuthScheme

from app.integrations.base import IntegrationProvider


class GoogleProvider(IntegrationProvider):
    name = "google"
    default_scopes = ("openid", "email")
    _auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    _token_url = "https://oauth2.googleapis.com/token"
    _revoke_url = "https://oauth2.googleapis.com/revoke"

    def auth_scheme(self) -> AuthScheme:
        return OAuth2(
            description="Google OAuth 2.0",
            flows=OAuthFlows(
                authorizationCode=OAuthFlowAuthorizationCode(
                    authorizationUrl=self._auth_url,
                    tokenUrl=self._token_url,
                    refreshUrl=self._token_url,
                    scopes={
                        "openid": "OpenID identity",
                        "email": "User email address",
                        "profile": "User profile info",
                        "https://www.googleapis.com/auth/drive.readonly": "Read Drive files",
                        "https://www.googleapis.com/auth/gmail.readonly": "Read Gmail",
                        "https://www.googleapis.com/auth/calendar.readonly": "Read Calendar",
                    },
                ),
            ),
        )
