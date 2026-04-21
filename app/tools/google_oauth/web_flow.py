"""Google OAuth2 Authorization Code + PKCE flow — replaces the device flow.

Device flow is rejected by Google for Gmail scopes (restricted allowlist).
This module uses the standard browser-redirect flow: user clicks an auth URL,
Google redirects them back to our /oauth/google/callback route with a code,
we exchange the code for tokens using PKCE, then persist per-user in token_store.

The callback route lives in app/a2a_server.py — it's the same Starlette app
that handles A2A requests, reusing the existing Cloudflare tunnel.
"""

import base64
import hashlib
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

_PENDING: dict[str, dict] = {}
_PENDING_TTL = 600  # 10 minutes


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _sweep_pending() -> None:
    now = time.time()
    for state in [s for s, v in _PENDING.items() if now - v["created_at"] > _PENDING_TTL]:
        del _PENDING[state]


def _get_redirect_uri() -> str:
    base = os.environ.get("OAUTH_BASE_URL", "").rstrip("/")
    if not base:
        raise ValueError("OAUTH_BASE_URL not configured in vault.")
    return f"{base}/oauth/google/callback"


def _get_client_config() -> tuple[str, str]:
    return (
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    )


def start_auth_flow(user_id: str) -> dict:
    """Begin a web-browser OAuth flow for the given user.

    Returns {"status": "success", "auth_url": ..., "state": ...} or
    {"status": "error", "message": ...}. The caller hands auth_url to the
    user to open in a browser; when Google redirects back, the callback
    route uses `state` to correlate the response.
    """
    client_id, _ = _get_client_config()
    if not client_id:
        return {"status": "error", "message": "GOOGLE_OAUTH_CLIENT_ID not configured. Set it via /init."}

    try:
        redirect_uri = _get_redirect_uri()
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    _sweep_pending()

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {
        "user_id": user_id,
        "code_verifier": verifier,
        "created_at": time.time(),
    }

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return {
        "status": "success",
        "auth_url": f"{AUTH_ENDPOINT}?{urlencode(params)}",
        "state": state,
    }


async def exchange_code(state: str, code: str) -> dict:
    """Exchange an authorization code for tokens.

    Called by the callback route. Returns {status, user_id, access_token,
    refresh_token, expires_in, email} on success.
    """
    pending = _PENDING.pop(state, None)
    if not pending:
        return {"status": "error", "message": "Invalid or unknown state parameter."}

    if time.time() - pending["created_at"] > _PENDING_TTL:
        return {"status": "error", "message": "Auth flow expired. Please start over."}

    client_id, client_secret = _get_client_config()
    if not client_id or not client_secret:
        return {"status": "error", "message": "OAuth client not configured."}

    try:
        redirect_uri = _get_redirect_uri()
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(TOKEN_ENDPOINT, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code_verifier": pending["code_verifier"],
            })
            data = resp.json()

        if "access_token" not in data:
            err = data.get("error_description") or data.get("error") or "Token exchange failed."
            return {"status": "error", "message": err}

        email = await _fetch_user_email(data["access_token"])
        return {
            "status": "success",
            "user_id": pending["user_id"],
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 3600),
            "email": email,
        }
    except Exception as e:
        return {"status": "error", "message": f"Token exchange error: {e}"}


async def _fetch_user_email(access_token: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            return resp.json().get("email", "unknown")
    except Exception:
        return "unknown"


async def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token. Works for web-flow tokens only
    (device-flow tokens are tied to a different client ID and will fail here)."""
    client_id, client_secret = _get_client_config()
    if not client_id or not client_secret:
        return {"status": "error", "message": "OAuth client not configured."}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(TOKEN_ENDPOINT, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            data = resp.json()

        if "access_token" not in data:
            err = data.get("error_description") or data.get("error") or "Refresh failed."
            return {"status": "error", "message": err}

        return {
            "status": "success",
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", 3600),
        }
    except Exception as e:
        return {"status": "error", "message": f"Refresh error: {e}"}
