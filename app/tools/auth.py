"""Tools for managing external platform integrations via OAuth2."""

import asyncio
import logging
import os
from typing import Dict, Any, Optional

from google.adk.tools.tool_context import ToolContext
from app.core.auth import auth_service
from app.core.transport import get_adapter

logger = logging.getLogger(__name__)

async def connect_to_platform(
    platform: str, 
    client_id: str, 
    client_secret: str, 
    scopes: list[str],
    tool_context: ToolContext
) -> Dict[str, Any]:
    """
    Starts an automated OAuth2 handshake for a given platform (google, github).
    The user will receive a code and instructions in the chat.
    
    Args:
        platform: The service to connect to ('google' or 'github').
        client_id: The Client ID for the application.
        client_secret: The Client Secret for the application.
        scopes: List of required API scopes.
    """
    platform = platform.lower()
    session_id = tool_context.session_id
    
    # We need the transport adapter to send proactive messages during polling
    # The tool_context doesn't directly have the adapter, but we can look it up
    # Note: This tool assumes we are in a session owned by a registered adapter.
    
    try:
        # 1. Request Device Code
        auth_data = await auth_service.start_device_flow(platform, client_id, scopes)
        
        user_code = auth_data["user_code"]
        verification_url = auth_data["verification_url"]
        device_code = auth_data["device_code"]
        interval = int(auth_data.get("interval", 5))
        expires_in = int(auth_data.get("expires_in", 1800))

        # 2. Return instructions to the user
        instructions = (
            f"🔗 **Action Required: Connect to {platform.capitalize()}**\n\n"
            f"1. Go to: {verification_url}\n"
            f"2. Enter this code: `{user_code}`\n\n"
            f"I am now waiting for your confirmation. This link expires in {expires_in // 60} minutes."
        )

        # 3. Launch background polling task
        # We don't want to block the agent's turn, so we run this in the background
        async def background_poll():
            try:
                await auth_service.poll_for_token(
                    platform, client_id, client_secret, device_code, interval, expires_in
                )
                
                # Notify the user on success
                # Try to find an adapter that can notify this session
                from app.core.transport import parse_notify_from_session_id
                info = parse_notify_from_session_id(session_id)
                if info:
                    adapter = get_adapter(info["type"])
                    if adapter:
                        await adapter.send_message(
                            info["chat_id"], 
                            f"✅ Successfully connected to **{platform.capitalize()}**! "
                            f"I now have access to your account with the requested scopes."
                        )
            except Exception as e:
                logger.error("OAuth background polling failed: %s", e)
                # Optionally notify user of failure

        asyncio.create_task(background_poll())

        return {
            "status": "success",
            "message": instructions
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


async def check_connection(platform: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Checks if a platform is already connected and authorized."""
    token = auth_service.get_token(platform.lower())
    if token:
        return {
            "status": "success",
            "connected": True,
            "message": f"Verified: I am currently connected to {platform.capitalize()}."
        }
    else:
        return {
            "status": "success",
            "connected": False,
            "message": f"I am NOT connected to {platform.capitalize()} yet."
        }
