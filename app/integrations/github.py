"""GitHub OAuth provider — repo + user access.

To use: register an OAuth app at https://github.com/settings/developers
and set vault keys
    OAUTH_GITHUB_CLIENT_ID
    OAUTH_GITHUB_CLIENT_SECRET
    OAUTH_GITHUB_REDIRECT_URI    # optional

GitHub's OAuth response is form-encoded by default; we send
`Accept: application/json` to get JSON back. This is handled by the base
class via the same Accept header.
"""

from __future__ import annotations

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.auth.auth_schemes import AuthScheme

from app.integrations.base import IntegrationProvider


class GitHubProvider(IntegrationProvider):
    name = "github"
    default_scopes = ("read:user",)
    _auth_url = "https://github.com/login/oauth/authorize"
    _token_url = "https://github.com/login/oauth/access_token"
    # GitHub requires a per-app revoke endpoint (DELETE on /applications/...);
    # leaving this empty so the default no-op revoke is used. Tools that need
    # revocation should call it directly via the GitHub API.
    _revoke_url = ""

    def auth_scheme(self) -> AuthScheme:
        return OAuth2(
            description="GitHub OAuth 2.0",
            flows=OAuthFlows(
                authorizationCode=OAuthFlowAuthorizationCode(
                    authorizationUrl=self._auth_url,
                    tokenUrl=self._token_url,
                    scopes={
                        "read:user": "Read user profile",
                        "user:email": "Read user email",
                        "repo": "Full repo access",
                        "public_repo": "Public repo access only",
                        "read:org": "Read org membership",
                    },
                ),
            ),
        )
