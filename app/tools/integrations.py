"""Integration configuration tools — flat tokens + OAuth providers.

Two flavors of "integration":

1. **Flat config keys** (legacy — Telegram/Slack tokens, raw API keys):
   `configure_integration(key_name)` arms a secure-capture for the user's
   next message; the transport layer intercepts it before the LLM sees it
   and writes to vault. `remove_integration(key_name)` deletes from vault.

2. **OAuth providers** (Phase D registry — Google, GitHub, future):
   `configure_integration(name)` starts the OAuth flow via
   `app.integrations.REGISTRY[name]`. The user receives an authorize URL,
   completes the flow externally, and the OAuth callback handler (on the
   A2A server, Phase G) finalizes the token via OriCredentialService.

`list_integrations` returns both surfaces so the agent (and user) can see
what's connected.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.integrations import REGISTRY as OAUTH_REGISTRY
from app.runtime.oauth_state import register_pending_oauth
from app.runtime.secure_capture import expect_key
from app.util.config import AGENT_CONFIG_KEYS

logger = logging.getLogger(__name__)


def _session_id(tool_context: ToolContext) -> str | None:
    if not tool_context:
        return None
    sess = getattr(tool_context, "session", None)
    if sess is None:
        return None
    return getattr(sess, "session_id", None) or getattr(sess, "id", None)


async def configure_integration(
    name: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Configure an integration. Routes by `name` to flat-token or OAuth flow.

    For flat tokens (e.g. GOOGLE_API_KEY): arms secure-capture; the user's
    next message is intercepted and saved to vault — never reaches the LLM.

    For OAuth providers (e.g. 'google', 'github'): builds an authorize URL
    and returns it to the user. The user completes the flow externally;
    the OAuth callback (on the A2A server) finalizes the token.
    """
    if not name:
        return {"status": "error", "message": "configure_integration requires a name"}

    name = name.strip()

    # OAuth path?
    if name.lower() in OAUTH_REGISTRY:
        provider = OAUTH_REGISTRY[name.lower()]
        if not provider.client_id():
            return {
                "status": "error",
                "error_code": "PROVIDER_NOT_CONFIGURED",
                "message": (
                    f"OAuth provider '{name}' has no client credentials. "
                    f"Set vault keys {name.upper()}_OAUTH_CLIENT_ID and "
                    f"{name.upper()}_OAUTH_CLIENT_SECRET first."
                ),
            }
        # Bind the random state token to the originating user+session BEFORE
        # issuing the authorize URL — the OAuth callback consumes this to
        # save the credential under the right user.
        sid = _session_id(tool_context)
        from app.plugins._common import state_user_id
        uid = state_user_id(tool_context) if (tool_context and getattr(tool_context, "state", None)) else "_global"
        if not sid:
            return {"status": "error", "message": "configure_integration requires a session_id"}
        state_token = secrets.token_urlsafe(24)
        register_pending_oauth(state_token, uid or "_global", sid)
        url = await provider.authorize_url(state=state_token)
        return {
            "status": "awaiting_user",
            "provider": name,
            "authorize_url": url,
            "state": state_token,
            "message": (
                f"To connect {name}, visit this authorize URL:\n{url}\n\n"
                "After you grant access, the OAuth callback finalizes the "
                "credential and stores it in the vault. No token ever passes "
                "through the chat."
            ),
        }

    # Flat-token path.
    key = name.upper()
    if key not in AGENT_CONFIG_KEYS:
        return {
            "status": "error",
            "error_code": "UNKNOWN_INTEGRATION",
            "message": (
                f"Unknown integration '{name}'. Known flat-token keys: "
                f"{sorted(AGENT_CONFIG_KEYS)}. Known OAuth providers: "
                f"{sorted(OAUTH_REGISTRY.keys())}."
            ),
        }
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "configure_integration requires a session_id"}
    expect_key(sid, key)
    return {
        "status": "awaiting_input",
        "key_name": key,
        "message": (
            f"Send your {key} in the next message. It will be captured "
            "securely — the message will be deleted from chat history and "
            "the value will NOT be seen by the AI."
        ),
    }


async def remove_integration(
    name: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Disconnect an integration. For flat tokens, deletes from vault. For
    OAuth, calls the provider's revoke endpoint and clears the cached
    credential."""
    if not name:
        return {"status": "error", "message": "remove_integration requires a name"}
    name = name.strip()

    # OAuth: revoke + clear vault.
    if name.lower() in OAUTH_REGISTRY:
        provider = OAUTH_REGISTRY[name.lower()]
        # Vault key namespace from OriCredentialService: OAUTH:<name>:<user_id>
        from app.plugins._common import state_user_id
        from deploy import vault
        user_id = (
            state_user_id(tool_context)
            if (tool_context and getattr(tool_context, "state", None))
            else "_global"
        ) or "_global"
        vault_key = f"OAUTH:{provider.name}:{user_id}"
        # Best-effort revoke — provider may have no endpoint.
        existing = vault.get(vault_key)
        if existing:
            try:
                import json as _json
                bundle = _json.loads(existing)
                access = (bundle.get("oauth2") or {}).get("access_token", "")
                if access:
                    await provider.revoke(access)
            except Exception:
                logger.debug("revoke failed; clearing vault key anyway")
        from deploy.vault import unset
        unset(vault_key)
        return {
            "status": "success",
            "message": f"Disconnected {name} (revoke attempted, credential cleared from vault).",
        }

    # Flat-token path.
    key = name.upper()
    if key not in AGENT_CONFIG_KEYS:
        return {
            "status": "error",
            "error_code": "UNKNOWN_INTEGRATION",
            "message": f"Unknown integration '{name}'.",
        }
    if key == "GOOGLE_API_KEY":
        return {
            "status": "error",
            "error_code": "REQUIRED_KEY",
            "message": "GOOGLE_API_KEY is required for embedding-based defenses; cannot remove.",
        }
    from deploy.vault import unset
    unset(key)
    return {"status": "success", "message": f"Removed {key}; integration disconnected."}


async def list_integrations(tool_context: ToolContext = None) -> dict[str, Any]:
    """List all integrations (flat tokens + OAuth providers) with status."""
    flat_status: dict[str, str] = {}
    for key in sorted(AGENT_CONFIG_KEYS):
        flat_status[key] = "connected" if os.environ.get(key) else "not configured"

    # OAuth status: present if vault has a credential under OAUTH:<name>:<user_id>.
    from app.plugins._common import state_user_id
    from deploy import vault
    user_id = (
        state_user_id(tool_context)
        if (tool_context and getattr(tool_context, "state", None))
        else "_global"
    ) or "_global"
    oauth_status: dict[str, str] = {}
    for name, provider in sorted(OAUTH_REGISTRY.items()):
        vault_key = f"OAUTH:{provider.name}:{user_id}"
        oauth_status[name] = "connected" if vault.get(vault_key) else "not configured"

    return {
        "status": "success",
        "flat_tokens": flat_status,
        "oauth_providers": oauth_status,
    }
