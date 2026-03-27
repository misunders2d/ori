import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
from google.adk.tools.tool_context import ToolContext




def configure_integration(key_name: str, tool_context: ToolContext) -> dict:
    """Initiates secure configuration of an integration key.

    This sets up a secure capture so the user's next message containing the key
    is intercepted at the transport level, saved directly to .env, and NEVER
    forwarded to the AI agent or stored in session history. The message is also
    deleted from Telegram chat history.

    IMPORTANT: After calling this tool, tell the user to send their key in the
    next message. Do NOT ask them to tell you the key — it will be captured securely.

    Args:
        key_name (str): The configuration key name. Must be one of: GOOGLE_API_KEY,
            TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, 
            GITHUB_TOKEN, GITHUB_REPO.

    Returns:
        dict: Status and instructions to relay to the user.
    """
    from app.app_utils.config import ALLOWED_CONFIG_KEYS

    key_name = key_name.strip().upper()

    if key_name not in ALLOWED_CONFIG_KEYS:
        return {
            "status": "error",
            "message": f"Unknown key: {key_name}. Allowed keys: {', '.join(sorted(ALLOWED_CONFIG_KEYS))}",
        }

    # Register pending capture using the session_id — works for any channel
    session = getattr(tool_context, "session", None)
    if session:
        sid = str(getattr(session, "id", ""))
        if sid:
            from app.secure_config import expect_key
            expect_key(sid, key_name)
            return {
                "status": "awaiting_input",
                "message": (
                    f"Send your {key_name} in the next message. "
                    f"It will be captured securely — your message will be deleted from chat "
                    f"and the key will NOT be seen by the AI or stored in conversation history."
                ),
            }

    return {
        "status": "error",
        "message": "Could not determine session. Please try again.",
    }



def remove_integration(key_name: str, tool_context: ToolContext) -> dict:
    """Removes a configuration key to disconnect an integration.

    Use this when the user wants to disconnect a service or remove an API key.

    Args:
        key_name (str): The configuration key name to remove.

    Returns:
        dict: Status of the operation.
    """
    from dotenv import unset_key

    from app.app_utils.config import ALLOWED_CONFIG_KEYS, ENV_FILE_PATH

    key_name = key_name.strip().upper()

    if key_name not in ALLOWED_CONFIG_KEYS:
        return {
            "status": "error",
            "message": f"Unknown key: {key_name}. Allowed keys: {', '.join(sorted(ALLOWED_CONFIG_KEYS))}",
        }

    if key_name == "GOOGLE_API_KEY":
        return {"status": "error", "message": "Cannot remove GOOGLE_API_KEY — it is required for the bot to function."}

    try:
        unset_key(ENV_FILE_PATH, key_name)
    except Exception:
        pass
    os.environ.pop(key_name, None)

    return {
        "status": "success",
        "message": f"Removed {key_name}. The integration is now disconnected.",
    }



def list_integrations(tool_context: ToolContext) -> dict:
    """Lists all integrations and their current connection status.

    Use this when the user asks what's configured, what integrations are available,
    or wants to see the current setup status.

    Returns:
        dict: Map of integration names to their status (connected/missing).
    """
    from app.app_utils.config import ALLOWED_CONFIG_KEYS

    integrations = {}
    for key in sorted(ALLOWED_CONFIG_KEYS):
        integrations[key] = "connected" if os.environ.get(key) else "not configured"

    return {"status": "success", "integrations": integrations}



