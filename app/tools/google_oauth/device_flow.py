"""Google OAuth2 Device Code flow for headless servers.

User gets a URL + code to enter on any browser. No callback URL needed.
Tokens are stored per-user in the token store.
"""

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _get_client_config() -> tuple[str, str]:
    """Get OAuth2 client ID and secret from environment."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    return client_id, client_secret


async def start_device_flow() -> dict:
    """Initiate the device code flow. Returns URL and code for the user."""
    client_id, _ = _get_client_config()
    if not client_id:
        return {"status": "error", "message": "GOOGLE_OAUTH_CLIENT_ID not configured. Set it via /init."}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_DEVICE_CODE_URL, data={
            "client_id": client_id,
            "scope": " ".join(SCOPES),
        })
        data = resp.json()

        if "error" in data:
            return {"status": "error", "message": f"Device flow error: {data.get('error_description', data['error'])}"}

        return {
            "status": "success",
            "verification_url": data["verification_url"],
            "user_code": data["user_code"],
            "device_code": data["device_code"],
            "expires_in": data.get("expires_in", 300),
            "interval": data.get("interval", 5),
        }


async def poll_for_token(device_code: str, interval: int = 5, timeout: int = 300) -> dict:
    """Poll Google until the user completes authorization or timeout."""
    client_id, client_secret = _get_client_config()
    if not client_id or not client_secret:
        return {"status": "error", "message": "GOOGLE_OAUTH_CLIENT_ID/SECRET not configured."}

    deadline = time.time() + timeout
    import asyncio

    async with httpx.AsyncClient(timeout=10) as client:
        while time.time() < deadline:
            resp = await client.post(_TOKEN_URL, data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant_type:device_code",
            })
            data = resp.json()

            if "access_token" in data:
                # Get the user's email from the token
                email = await _get_email_from_token(data["access_token"])
                return {
                    "status": "success",
                    "access_token": data["access_token"],
                    "refresh_token": data.get("refresh_token", ""),
                    "expires_in": data.get("expires_in", 3600),
                    "email": email,
                }

            error = data.get("error")
            if error == "authorization_pending":
                await asyncio.sleep(interval)
                continue
            elif error == "slow_down":
                interval += 2
                await asyncio.sleep(interval)
                continue
            elif error == "expired_token":
                return {"status": "error", "message": "Device code expired. Please try again."}
            elif error == "access_denied":
                return {"status": "error", "message": "User denied access."}
            else:
                return {"status": "error", "message": f"Token error: {data.get('error_description', error)}"}

    return {"status": "error", "message": "Timed out waiting for user authorization."}


async def refresh_access_token(refresh_token: str) -> dict:
    """Use a refresh token to get a new access token."""
    client_id, client_secret = _get_client_config()
    if not client_id or not client_secret:
        return {"status": "error", "message": "GOOGLE_OAUTH_CLIENT_ID/SECRET not configured."}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(_TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        data = resp.json()

        if "access_token" in data:
            return {
                "status": "success",
                "access_token": data["access_token"],
                "expires_in": data.get("expires_in", 3600),
            }
        return {"status": "error", "message": f"Refresh failed: {data.get('error_description', data.get('error'))}"}


async def _get_email_from_token(access_token: str) -> str:
    """Fetch the authenticated user's email from the token."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = resp.json()
            return data.get("email", "unknown")
    except Exception:
        return "unknown"
