"""
Universal OAuth2 service supporting Device Code and Authorization Code + PKCE flows.

No hardcoded platforms — any OAuth2-compliant provider can be registered at runtime.
Platform configs and tokens are persisted to JSON files in the data directory.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

logger = logging.getLogger(__name__)

PLATFORMS_PATH = os.path.abspath("./data/oauth_platforms.json")
TOKENS_PATH = os.path.abspath("./data/auth_tokens.json")


class OAuthService:
    """Universal OAuth2 service. Supports any provider via a config-driven platform registry."""

    def __init__(self):
        self._platforms = self._load_json(PLATFORMS_PATH)
        self._tokens = self._load_json(TOKENS_PATH)
        self._pending_pkce: dict[str, dict[str, str]] = {}

    @staticmethod
    def _load_json(path: str) -> dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                logger.error("Failed to load %s", path)
        return {}

    @staticmethod
    def _save_json(path: str, data: dict[str, Any]):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    # -----------------------------------------------------------------------
    # Platform Registry
    # -----------------------------------------------------------------------

    def register_platform(self, platform_id: str, config: dict[str, Any]):
        """Register or update a platform configuration."""
        required = {"name", "flow", "token_endpoint"}
        missing = required - set(config.keys())
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

        flow = config["flow"]
        if flow not in ("device_code", "auth_code_pkce"):
            raise ValueError(f"Unsupported flow: {flow}. Use 'device_code' or 'auth_code_pkce'.")
        if flow == "device_code" and "device_code_endpoint" not in config:
            raise ValueError("Device Code flow requires 'device_code_endpoint'.")
        if flow == "auth_code_pkce" and "auth_endpoint" not in config:
            raise ValueError("Auth Code + PKCE flow requires 'auth_endpoint'.")

        self._platforms[platform_id] = config
        self._save_json(PLATFORMS_PATH, self._platforms)

    def remove_platform(self, platform_id: str):
        """Remove a platform and its stored tokens."""
        self._platforms.pop(platform_id, None)
        self._tokens.pop(platform_id, None)
        self._pending_pkce.pop(platform_id, None)
        self._save_json(PLATFORMS_PATH, self._platforms)
        self._save_json(TOKENS_PATH, self._tokens)

    def get_platform(self, platform_id: str) -> dict[str, Any] | None:
        return self._platforms.get(platform_id)

    def list_platforms(self) -> dict[str, Any]:
        result = {}
        for pid, cfg in self._platforms.items():
            token_info = self._tokens.get(pid, {})
            connected = bool(token_info.get("access_token"))
            expired = False
            if connected and token_info.get("expires_at"):
                expired = time.time() >= token_info["expires_at"]
            result[pid] = {
                "name": cfg.get("name"),
                "flow": cfg.get("flow"),
                "connected": connected and not expired,
                "has_refresh_token": bool(token_info.get("refresh_token")),
            }
        return result

    # -----------------------------------------------------------------------
    # Token Management
    # -----------------------------------------------------------------------

    async def get_token(self, platform_id: str) -> str | None:
        """Returns a valid access token, auto-refreshing if needed."""
        token_data = self._tokens.get(platform_id)
        if not token_data or not token_data.get("access_token"):
            return None

        expires_at = token_data.get("expires_at", 0)
        # Refresh if expiring within 5 minutes
        if time.time() >= expires_at - 300:
            if token_data.get("refresh_token"):
                try:
                    await self._refresh_token(platform_id)
                    token_data = self._tokens.get(platform_id, {})
                except Exception as e:
                    logger.warning("Token refresh failed for %s: %s", platform_id, e)
                    return None
            else:
                logger.warning("Token expired for %s and no refresh token available.", platform_id)
                return None

        return token_data.get("access_token")

    async def _refresh_token(self, platform_id: str):
        """Refresh an expired access token using the stored refresh token."""
        platform = self._platforms.get(platform_id)
        token_data = self._tokens.get(platform_id)
        if not platform or not token_data:
            raise ValueError(f"No config or token data for {platform_id}")

        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise ValueError(f"No refresh token for {platform_id}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(platform["token_endpoint"], data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": platform.get("client_id", ""),
                "client_secret": platform.get("client_secret", ""),
            })
            resp.raise_for_status()
            data = resp.json()

        if "access_token" not in data:
            raise Exception(f"Refresh response missing access_token: {data}")

        self._tokens[platform_id] = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": time.time() + data.get("expires_in", 3600),
            "scopes": data.get("scope", "").split() if data.get("scope") else token_data.get("scopes", []),
        }
        self._save_json(TOKENS_PATH, self._tokens)
        logger.info("Token refreshed for %s", platform_id)

    def _store_token(self, platform_id: str, data: dict[str, Any]):
        """Store token data from a successful auth response."""
        self._tokens[platform_id] = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + data.get("expires_in", 3600),
            "scopes": data.get("scope", "").split() if data.get("scope") else [],
        }
        self._save_json(TOKENS_PATH, self._tokens)

    def disconnect(self, platform_id: str):
        """Remove stored tokens for a platform."""
        self._tokens.pop(platform_id, None)
        self._pending_pkce.pop(platform_id, None)
        self._save_json(TOKENS_PATH, self._tokens)

    # -----------------------------------------------------------------------
    # Device Code Flow
    # -----------------------------------------------------------------------

    async def start_device_flow(self, platform_id: str, scopes: list[str] | None = None) -> dict[str, Any]:
        """Initiate Device Code Flow. Returns device code data with user instructions."""
        platform = self._platforms.get(platform_id)
        if not platform:
            raise ValueError(f"Platform '{platform_id}' not registered.")
        if platform["flow"] != "device_code":
            raise ValueError(f"Platform '{platform_id}' uses '{platform['flow']}', not 'device_code'.")

        effective_scopes = scopes or platform.get("default_scopes", [])

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(platform["device_code_endpoint"], data={
                "client_id": platform.get("client_id", ""),
                "scope": " ".join(effective_scopes),
            })

            content_type = resp.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                data = {k: v[0] for k, v in parse_qs(resp.text).items()}
            else:
                data = resp.json()

        if "device_code" not in data:
            raise Exception(f"Device flow initiation failed: {data.get('error_description', data)}")

        return data

    async def poll_for_token(
        self, platform_id: str, device_code: str, interval: int, expires_in: int
    ) -> dict[str, Any]:
        """Poll token endpoint until the user authorizes or the request expires."""
        platform = self._platforms.get(platform_id)
        if not platform:
            raise ValueError(f"Platform '{platform_id}' not registered.")

        start_time = time.time()

        async with httpx.AsyncClient(timeout=15.0) as client:
            while time.time() - start_time < expires_in:
                resp = await client.post(platform["token_endpoint"], data={
                    "client_id": platform.get("client_id", ""),
                    "client_secret": platform.get("client_secret", ""),
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }, headers={"Accept": "application/json"})

                data = resp.json()
                if "access_token" in data:
                    self._store_token(platform_id, data)
                    return data

                error = data.get("error")
                if error == "authorization_pending":
                    await asyncio.sleep(interval)
                elif error == "slow_down":
                    interval += 5
                    await asyncio.sleep(interval)
                else:
                    raise Exception(f"Auth failed: {data.get('error_description', error)}")

        raise Exception("Authentication timed out.")

    # -----------------------------------------------------------------------
    # Authorization Code + PKCE Flow
    # -----------------------------------------------------------------------

    @staticmethod
    def _generate_pkce() -> tuple[str, str]:
        """Generate PKCE code_verifier and code_challenge (S256)."""
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        return code_verifier, code_challenge

    def start_auth_code_flow(
        self, platform_id: str, scopes: list[str] | None = None, redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob"
    ) -> str:
        """Generate an authorization URL for Auth Code + PKCE. Returns the URL for the user to visit."""
        platform = self._platforms.get(platform_id)
        if not platform:
            raise ValueError(f"Platform '{platform_id}' not registered.")
        if platform["flow"] != "auth_code_pkce":
            raise ValueError(f"Platform '{platform_id}' uses '{platform['flow']}', not 'auth_code_pkce'.")

        effective_scopes = scopes or platform.get("default_scopes", [])
        code_verifier, code_challenge = self._generate_pkce()
        state = secrets.token_urlsafe(16)

        self._pending_pkce[platform_id] = {
            "code_verifier": code_verifier,
            "state": state,
            "redirect_uri": redirect_uri,
        }

        params = {
            "client_id": platform.get("client_id", ""),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(effective_scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        # Extra params some providers need (e.g., access_type=offline for Google refresh tokens)
        extra = platform.get("extra_auth_params", {})
        params.update(extra)

        return f"{platform['auth_endpoint']}?{urlencode(params)}"

    def has_pending_auth_code(self, platform_id: str) -> bool:
        """Check if a platform has an in-progress Auth Code + PKCE flow."""
        return platform_id in self._pending_pkce

    async def exchange_auth_code(self, platform_id: str, code: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens, completing the PKCE flow."""
        platform = self._platforms.get(platform_id)
        if not platform:
            raise ValueError(f"Platform '{platform_id}' not registered.")

        pkce_data = self._pending_pkce.pop(platform_id, None)
        if not pkce_data:
            raise ValueError(f"No pending auth flow for '{platform_id}'. Start the flow first.")

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(platform["token_endpoint"], data={
                "client_id": platform.get("client_id", ""),
                "client_secret": platform.get("client_secret", ""),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": pkce_data["redirect_uri"],
                "code_verifier": pkce_data["code_verifier"],
            })
            resp.raise_for_status()
            data = resp.json()

        if "access_token" not in data:
            raise Exception(f"Token exchange failed: {data}")

        self._store_token(platform_id, data)
        return data


# Global instance
auth_service = OAuthService()
