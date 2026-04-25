"""Universal OAuth2 tools for connecting any external platform."""

import asyncio
import logging
from typing import Dict, Any

from google.adk.tools.tool_context import ToolContext
from app.runtime.auth import auth_service

logger = logging.getLogger(__name__)


def list_platforms(tool_context: ToolContext) -> Dict[str, Any]:
    """List all registered OAuth2 platforms and their connection status."""
    platforms = auth_service.list_platforms()
    if not platforms:
        return {
            "status": "success",
            "message": "No platforms registered. Use register_platform to add one.",
            "platforms": {},
        }
    return {"status": "success", "platforms": platforms}


async def register_platform(
    platform_id: str,
    name: str,
    flow: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    auth_endpoint: str,
    device_code_endpoint: str,
    default_scopes: list[str],
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """
    Register a new OAuth2 platform for authentication. Supports any OAuth2-compliant provider.

    Args:
        platform_id: Short unique identifier (e.g., 'google', 'dropbox', 'github', 'microsoft').
        name: Human-readable name (e.g., 'Google', 'Dropbox').
        flow: OAuth flow type. Use 'device_code' for headless-friendly providers (Google, GitHub, Microsoft) or 'auth_code_pkce' for others (Dropbox, Spotify).
        token_endpoint: URL for token exchange and refresh.
        client_id: OAuth2 Client ID from the provider's developer console.
        client_secret: OAuth2 Client Secret. Use empty string if the provider does not require one.
        auth_endpoint: Authorization URL. Required for 'auth_code_pkce' flow. Set to empty string for 'device_code'.
        device_code_endpoint: Device code request URL. Required for 'device_code' flow. Set to empty string for 'auth_code_pkce'.
        default_scopes: Default permission scopes to request during authentication.
    """
    try:
        config: Dict[str, Any] = {
            "name": name,
            "flow": flow,
            "token_endpoint": token_endpoint,
            "client_id": client_id,
            "client_secret": client_secret,
            "default_scopes": default_scopes,
        }
        if auth_endpoint:
            config["auth_endpoint"] = auth_endpoint
        if device_code_endpoint:
            config["device_code_endpoint"] = device_code_endpoint

        auth_service.register_platform(platform_id, config)
        return {
            "status": "success",
            "message": f"Platform '{name}' registered as '{platform_id}' ({flow} flow).",
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error("Failed to register platform %s: %s", platform_id, e)
        return {"status": "error", "message": f"Registration failed: {e}"}


async def connect_to_platform(
    platform_id: str,
    scopes: list[str],
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """
    Start OAuth2 authentication for a registered platform.
    The user will receive instructions to authorize in their browser.

    Args:
        platform_id: The platform identifier (e.g., 'google', 'dropbox'). Must be registered first.
        scopes: Permission scopes to request. Send an empty list to use the platform's default scopes.
    """
    platform = auth_service.get_platform(platform_id)
    if not platform:
        available = ", ".join(auth_service.list_platforms().keys()) or "none"
        return {
            "status": "error",
            "message": f"Platform '{platform_id}' not registered. Available: {available}.",
        }

    flow = platform["flow"]
    
    session_id = ""
    session = getattr(tool_context, "session", None)
    if session:
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or ""
        
    effective_scopes = scopes if scopes else None

    try:
        if flow == "device_code":
            return await _start_device_code_flow(platform_id, platform, effective_scopes, session_id)
        elif flow == "auth_code_pkce":
            return _start_auth_code_flow(platform_id, platform, effective_scopes)
        else:
            return {"status": "error", "message": f"Unknown flow type: {flow}"}
    except Exception as e:
        logger.error("Auth initiation failed for %s: %s", platform_id, e)
        return {"status": "error", "message": f"Authentication failed: {e}"}


async def _start_device_code_flow(
    platform_id: str, platform: dict, scopes: list[str] | None, session_id: str
) -> Dict[str, Any]:
    """Handle Device Code Flow: get codes, return instructions, poll in background."""
    auth_data = await auth_service.start_device_flow(platform_id, scopes)

    user_code = auth_data.get("user_code", "")
    verification_url = auth_data.get("verification_url") or auth_data.get("verification_uri", "")
    device_code = auth_data["device_code"]
    interval = int(auth_data.get("interval", 5))
    expires_in = int(auth_data.get("expires_in", 1800))

    async def _poll():
        try:
            await auth_service.poll_for_token(platform_id, device_code, interval, expires_in)
            from app.runtime.transport import parse_notify_from_session_id, get_adapter
            info = parse_notify_from_session_id(session_id)
            if info:
                adapter = get_adapter(info["type"])
                if adapter:
                    await adapter.send_message(
                        info["chat_id"],
                        f"Connected to {platform['name']} successfully.",
                    )
        except Exception as e:
            logger.error("OAuth polling failed for %s: %s", platform_id, e)

    asyncio.create_task(_poll())

    return {
        "status": "success",
        "message": (
            f"**Connect to {platform['name']}**\n\n"
            f"1. Go to: {verification_url}\n"
            f"2. Enter code: `{user_code}`\n\n"
            f"Waiting for authorization (expires in {expires_in // 60} minutes)."
        ),
    }


def _start_auth_code_flow(
    platform_id: str, platform: dict, scopes: list[str] | None
) -> Dict[str, Any]:
    """Handle Auth Code + PKCE Flow: generate URL, return instructions."""
    auth_url = auth_service.start_auth_code_flow(platform_id, scopes)

    return {
        "status": "awaiting_code",
        "platform_id": platform_id,
        "message": (
            f"**Connect to {platform['name']}**\n\n"
            f"1. Open this URL in your browser:\n{auth_url}\n\n"
            f"2. Authorize the application.\n"
            f"3. Copy the authorization code you receive and send it back to me."
        ),
    }


async def complete_auth_code(
    platform_id: str,
    code: str,
    tool_context: ToolContext,
) -> Dict[str, Any]:
    """
    Complete an Authorization Code + PKCE flow by exchanging the code for tokens.
    Call this after the user has authorized and returned the code.

    Args:
        platform_id: The platform being authenticated.
        code: The authorization code from the provider's redirect or confirmation page.
    """
    try:
        await auth_service.exchange_auth_code(platform_id, code.strip())
        platform = auth_service.get_platform(platform_id)
        name = platform["name"] if platform else platform_id
        return {"status": "success", "message": f"Connected to {name} successfully."}
    except Exception as e:
        logger.error("Auth code exchange failed for %s: %s", platform_id, e)
        return {"status": "error", "message": f"Authorization failed: {e}"}


async def check_connection(platform_id: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Check if a platform is connected and has a valid (non-expired) token.

    Args:
        platform_id: The platform to check (e.g., 'google', 'dropbox').
    """
    platform = auth_service.get_platform(platform_id)
    if not platform:
        return {"status": "error", "message": f"Platform '{platform_id}' not registered."}

    token = await auth_service.get_token(platform_id)
    if token:
        return {
            "status": "success",
            "connected": True,
            "message": f"Connected to {platform['name']} with a valid token.",
        }
    return {
        "status": "success",
        "connected": False,
        "message": f"Not connected to {platform['name']}. Use connect_to_platform to authenticate.",
    }


async def disconnect_platform(platform_id: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Disconnect from a platform by removing its stored tokens.

    Args:
        platform_id: The platform to disconnect from.
    """
    platform = auth_service.get_platform(platform_id)
    if not platform:
        return {"status": "error", "message": f"Platform '{platform_id}' not registered."}

    auth_service.disconnect(platform_id)
    return {"status": "success", "message": f"Disconnected from {platform['name']}. Tokens removed."}


async def remove_platform_registration(platform_id: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Completely remove a platform registration and all its stored tokens and credentials.

    Args:
        platform_id: The platform to remove.
    """
    platform = auth_service.get_platform(platform_id)
    if not platform:
        return {"status": "error", "message": f"Platform '{platform_id}' not registered."}

    name = platform["name"]
    auth_service.remove_platform(platform_id)
    return {"status": "success", "message": f"Removed '{name}' ({platform_id}) and all associated credentials."}
