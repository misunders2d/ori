import asyncio
import json
import logging
import os
import time
from typing import Optional, Dict, Any

import httpx

logger = logging.getLogger(__name__)

AUTH_DATA_PATH = os.path.abspath("./data/auth_tokens.json")

class OAuthService:
    """Manages OAuth2 flows and token persistence for headless environments."""

    def __init__(self):
        self._tokens = self._load_tokens()
        self._active_sessions = {}  # session_id -> auth_info

    def _load_tokens(self) -> Dict[str, Any]:
        if os.path.exists(AUTH_DATA_PATH):
            try:
                with open(AUTH_DATA_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                logger.error("Failed to load auth tokens from %s", AUTH_DATA_PATH)
        return {}

    def _save_tokens(self):
        os.makedirs(os.path.dirname(AUTH_DATA_PATH), exist_ok=True)
        with open(AUTH_DATA_PATH, "w") as f:
            json.dump(self._tokens, f, indent=2)

    def get_token(self, platform: str) -> Optional[str]:
        """Returns the access token for a platform, if available."""
        return self._tokens.get(platform, {}).get("access_token")

    async def start_device_flow(self, platform: str, client_id: str, scopes: list[str]) -> Dict[str, Any]:
        """Initiates a Device Code Flow for platforms that support it (Google, GitHub)."""
        
        endpoints = {
            "google": "https://oauth2.googleapis.com/device/code",
            "github": "https://github.com/login/device/code"
        }
        
        if platform not in endpoints:
            raise ValueError(f"Platform {platform} does not support device flow.")

        async with httpx.AsyncClient() as client:
            resp = await client.post(endpoints[platform], data={
                "client_id": client_id,
                "scope": " ".join(scopes)
            })
            
            # GitHub returns URL-encoded form data by default, Google returns JSON
            if platform == "github":
                from urllib.parse import parse_qs
                data = {k: v[0] for k, v in parse_qs(resp.text).items()}
            else:
                data = resp.json()

            if "device_code" not in data:
                logger.error("Failed to initiate device flow for %s: %s", platform, resp.text)
                raise Exception(f"Failed to initiate auth: {data.get('error_description', 'Unknown error')}")

            return data

    async def poll_for_token(
        self, 
        platform: str, 
        client_id: str, 
        client_secret: str, 
        device_code: str, 
        interval: int,
        expires_in: int
    ) -> Dict[str, Any]:
        """Polls the token endpoint until authorized or expired."""
        
        token_endpoints = {
            "google": "https://oauth2.googleapis.com/token",
            "github": "https://github.com/login/oauth/access_token"
        }
        
        grant_type = "urn:ietf:params:oauth:grant-type:device_code"
        start_time = time.time()
        
        async with httpx.AsyncClient() as client:
            while time.time() - start_time < expires_in:
                resp = await client.post(token_endpoints[platform], data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "device_code": device_code,
                    "grant_type": grant_type
                }, headers={"Accept": "application/json"})
                
                data = resp.json()
                
                if "access_token" in data:
                    # Save token
                    self._tokens[platform] = {
                        "access_token": data["access_token"],
                        "refresh_token": data.get("refresh_token"),
                        "expires_at": time.time() + data.get("expires_in", 3600),
                        "scopes": data.get("scope", "").split(" ")
                    }
                    self._save_tokens()
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

# Global instance
auth_service = OAuthService()
