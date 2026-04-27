"""A2A server — wraps the App's root agent with `to_a2a` and adds:

- API-key middleware (`x-a2a-api-key` header required for non-discovery paths).
- Address-update endpoint (`POST /a2a/address-update`) — deterministic
  friend URL update on tunnel rotation, no LLM involvement.
- OAuth callback handler (`GET /oauth/<provider>/callback`) — finalizes
  the OAuth flow when the user returns from the authorize URL.

Multimodal-aware: the agent card declares text + binary input/output
modes (DNA bundles ride as `application/gzip`; images/audio/video also
supported). Per Phase F, `app.tools.a2a` packs binary as `Part.from_bytes`
inline so the legacy public DNA download URL is no longer needed.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime
from typing import Any

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.agent import root_agent  # respects current App.root_agent (Workflow in F)

logger = logging.getLogger(__name__)


# Discovery paths remain publicly accessible.
_PUBLIC_PATHS = {
    "/.well-known/agent.json",
    "/.well-known/agent-card.json",
}

# Unguarded HTTP routes (still require API key; not LLM-routed though).
_OOB_ROUTES_PREFIXES = ("/oauth/", "/a2a/address-update")


FRIENDS_FILE = os.path.abspath("./data/friends.json")
KEYS_FILE = os.path.abspath("./data/a2a_keys.json")


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class A2AApiKeyMiddleware(BaseHTTPMiddleware):
    """Require `x-a2a-api-key` header on non-discovery endpoints."""

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request, call_next):
        path = request.url.path

        if path in _PUBLIC_PATHS:
            return await call_next(request)

        # OAuth callback — browser redirect from the provider's consent page,
        # cannot carry the API key header. CSRF is enforced via the `state`
        # parameter (bound to user+session at issue-time in
        # `app/integrations/base.py:authorize_url`). Short-circuit BEFORE
        # the API-key check or the callback always 401s. Matches legacy.
        if path.startswith("/oauth/") and path.endswith("/callback") and request.method == "GET":
            return await _handle_oauth_callback(request)

        provided = request.headers.get("x-a2a-api-key", "")
        if provided != self.api_key:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: invalid or missing x-a2a-api-key header",
                    },
                    "id": None,
                },
                status_code=401,
            )

        # Out-of-band routes (not LLM-routed): handle here and short-circuit.
        if path == "/a2a/address-update" and request.method == "POST":
            return await _handle_address_update(request)

        return await call_next(request)


# ---------------------------------------------------------------------------
# Address-update handler (carry-forward, paths unchanged)
# ---------------------------------------------------------------------------

async def _handle_address_update(request) -> JSONResponse:
    """Update a friend's URL when they broadcast a new tunnel address.

    Authenticated via `x-a2a-api-key` (already validated by middleware).
    Body: `{"sender_name": "...", "new_base_url": "https://..."}`.
    """
    try:
        body = await request.json()
        sender_name = body.get("sender_name", "")
        new_url = body.get("new_base_url", "").rstrip("/")
        if not sender_name or not new_url:
            return JSONResponse(
                {"status": "error", "message": "Missing sender_name or new_base_url"},
                status_code=400,
            )
        if not os.path.exists(FRIENDS_FILE):
            return JSONResponse({"status": "ignored", "message": "No friends registered"})
        with open(FRIENDS_FILE) as f:
            friends = json.load(f)
        sender_api_key = request.headers.get("x-a2a-api-key", "")
        stored_keys: dict[str, str] = {}
        if os.path.exists(KEYS_FILE):
            try:
                with open(KEYS_FILE) as f:
                    stored_keys = json.load(f)
            except Exception:
                pass
        # Match by API key (most reliable) → fallback to name.
        matched_key: str | None = None
        if sender_api_key:
            for nickname, key in stored_keys.items():
                if key == sender_api_key and nickname in friends:
                    matched_key = nickname
                    break
        if not matched_key:
            for key, data in friends.items():
                if (
                    key.lower() == sender_name.lower()
                    or data.get("name", "").lower() == sender_name.lower()
                ):
                    matched_key = key
                    break
        if not matched_key:
            logger.info("Address update from unknown sender '%s' — ignored", sender_name)
            return JSONResponse({"status": "ignored", "message": f"Unknown sender: {sender_name}"})
        old_url = friends[matched_key].get("base_url", "")
        friends[matched_key]["base_url"] = new_url
        friends[matched_key]["endpoint_url"] = new_url
        friends[matched_key]["last_address_update"] = datetime.now().isoformat()
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)
        logger.info("Auto-updated friend '%s' URL: %s -> %s", matched_key, old_url, new_url)
        return JSONResponse(
            {"status": "success", "message": f"Updated {matched_key} to {new_url}"}
        )
    except Exception as e:
        logger.error("Address update handler failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# OAuth callback handler — completes the flow started by tools/integrations.py
# ---------------------------------------------------------------------------

async def _handle_oauth_callback(request) -> Response:
    """Handle GET /oauth/<provider>/callback?code=...&state=...

    Two flows can issue OAuth state tokens, and both come back to this
    same callback URL. Try them in order:

    1. **Per-user OAuth** (legacy `app/tools/google_oauth/web_flow.py`,
       used for Drive/Gmail/Calendar token store). State stored in
       `web_flow._PENDING`. This path is NOT admin-gated — any whitelisted
       user can complete it. Matches legacy amazon_manager behavior.
    2. **Integrations subsystem OAuth** (`app/integrations/REGISTRY`,
       used by `configure_integration`). State stored via
       `_consume_pending_oauth`. This path IS admin-gated at the tool
       level (configure_integration) so only admins reach it.

    For both paths we render an HTML success/failure page rather than
    JSON, since the user lands here via a browser redirect.
    """
    from starlette.responses import HTMLResponse

    params = request.query_params
    state = params.get("state", "")
    code = params.get("code", "")
    error = params.get("error", "")

    if error:
        return HTMLResponse(
            _oauth_page("Authorization denied", f"Provider returned: {error}"),
            status_code=400,
        )
    if not state or not code:
        return HTMLResponse(
            _oauth_page("Invalid callback", "Missing state or code parameter."),
            status_code=400,
        )

    # ---- Path 1: per-user OAuth via web_flow (legacy, non-admin friendly) ----
    try:
        from app.tools.google_oauth import web_flow
        from app.tools.google_oauth.token_store import save_token, save_user_mapping

        # web_flow.exchange_code pops state from web_flow._PENDING — returns
        # {"status": "error", ...} when the state isn't from this path.
        result = await web_flow.exchange_code(state, code)
    except Exception as e:
        logger.exception("OAuth callback: web_flow.exchange_code raised")
        result = {"status": "error", "message": str(e)}

    if result.get("status") == "success":
        try:
            user_id = result["user_id"]
            email = result["email"]
            save_token(
                email,
                result["access_token"],
                result.get("refresh_token", ""),
                result.get("expires_in", 3600),
                web_flow.SCOPES,
            )
            if user_id and user_id != email:
                save_user_mapping(user_id, email)
            logger.info("OAuth connect complete for %s (user_id=%s)", email, user_id)
            return HTMLResponse(_oauth_page(
                "Connected!",
                f"Google account <strong>{email}</strong> is now connected. "
                f"You can close this tab and return to the chat.",
            ))
        except Exception as e:
            logger.exception("OAuth callback: failed to persist per-user token")
            return HTMLResponse(
                _oauth_page("Storage failed", str(e)),
                status_code=500,
            )

    # ---- Path 2: integrations subsystem (admin-gated configure_integration) --
    try:
        path_parts = request.url.path.strip("/").split("/")
        if len(path_parts) < 3:
            return HTMLResponse(
                _oauth_page("Invalid callback", "Malformed callback path."),
                status_code=400,
            )
        provider_name = path_parts[1]
        from app.integrations import REGISTRY
        provider = REGISTRY.get(provider_name)
        if provider is None:
            return HTMLResponse(
                _oauth_page(
                    "Unknown provider",
                    f"OAuth provider '{provider_name}' is not registered.",
                ),
                status_code=404,
            )
        binding = _consume_pending_oauth(state)
        if binding is None:
            logger.warning("OAuth callback: state token %s not in any pending map", state)
            return HTMLResponse(
                _oauth_page(
                    "Authorization expired",
                    "The OAuth state token was not recognized. Start the flow again.",
                ),
                status_code=400,
            )
        cred = await provider.exchange_code(code)
        user_id = binding.get("user_id", "_global")
        from deploy import vault
        vault_key = f"OAUTH:{provider.name}:{user_id}"
        vault.set(vault_key, cred.model_dump_json(exclude_none=True))
        logger.info("OAuth callback: saved credential under %s", vault_key)
        return HTMLResponse(_oauth_page(
            "Connected!",
            f"{provider.name} is connected. You can close this tab and return "
            f"to the chat.",
        ))
    except Exception as e:
        logger.exception("OAuth callback handler failed (integrations path)")
        return HTMLResponse(
            _oauth_page("Authorization failed", str(e)),
            status_code=500,
        )


def _oauth_page(title: str, body_html: str) -> str:
    """Minimal HTML page for browser-rendered OAuth callbacks. Matches legacy."""
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:560px;"
        "margin:80px auto;padding:0 20px;color:#222;}"
        "h1{font-size:24px;margin-bottom:12px;}p{line-height:1.5;color:#444;}"
        "</style></head><body>"
        f"<h1>{title}</h1><p>{body_html}</p></body></html>"
    )


# Pending OAuth state map lives in `app.runtime.oauth_state` so both this
# module and `tools/integrations.py` can use it without triggering each
# other's heavy imports.
from app.runtime.oauth_state import (  # deliberate placement; see comment above
    consume_pending_oauth as _consume_pending_oauth,
)

# ---------------------------------------------------------------------------
# Agent card — multimodal modes
# ---------------------------------------------------------------------------

def _build_agent_card() -> dict:
    """Build the v1.0-compliant A2A Agent Card from environment + REGISTRY."""
    bot_name = os.environ.get("BOT_NAME", "Ori")
    base_url = os.environ.get("A2A_BASE_URL", "http://localhost:8000")
    api_key_set = bool(os.environ.get("A2A_API_KEY"))

    card: dict[str, Any] = {
        "id": os.environ.get("A2A_AGENT_ID", f"ori-{bot_name.lower()}"),
        "name": bot_name,
        "version": "2.0.0",
        "description": "An autonomous self-evolving digital organism.",
        "url": base_url,
        "provider": {
            "organization": os.environ.get("A2A_PROVIDER_NAME", "Ori Project"),
            "url": os.environ.get("A2A_PROVIDER_URL", base_url),
        },
        # Multimodal. DNA bundles ride as application/gzip; we also accept
        # images/audio/video/PDF inline. Tools that need a particular type
        # check the part's mime_type.
        "defaultInputModes": [
            "text/plain",
            "application/json",
            "application/octet-stream",
            "application/gzip",
            "application/pdf",
            "image/png", "image/jpeg", "image/webp",
            "audio/mpeg", "audio/wav", "audio/ogg",
            "video/mp4",
        ],
        "defaultOutputModes": [
            "text/plain",
            "application/gzip",
            "application/octet-stream",
            "image/png", "image/jpeg",
            "audio/mpeg",
        ],
        "endpoints": [
            {"type": "json-rpc", "url": base_url},
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "multiTurn": True,
            "extendedAgentCard": False,
        },
        "skills": [
            {
                "id": "general-conversation",
                "name": "general-conversation",
                "description": "General-purpose conversation, scheduling, memory.",
                "tags": ["conversation", "scheduling"],
            },
            {
                "id": "self-evolution",
                "name": "self-evolution",
                "description": "Analyzes and improves its own source code through sandboxed evolution.",
                "tags": ["evolution", "code"],
            },
            {
                "id": "web-research",
                "name": "web-research",
                "description": "Searches the web and fetches content for research tasks.",
                "tags": ["research", "web"],
            },
            {
                "id": "dna-exchange",
                "name": "dna-exchange",
                "description": (
                    "Exchanges sanitized technical improvements (tools/skills) "
                    "with other Ori instances via inline binary A2A messages."
                ),
                "tags": ["a2a", "exchange", "dna"],
            },
        ],
    }
    if api_key_set:
        card["securitySchemes"] = {
            "apiKey": {"type": "apiKey", "in": "header", "name": "x-a2a-api-key"},
        }
        card["security"] = [{"apiKey": []}]
    return card


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------

a2a_app = None


def build_a2a_app():
    """Return a Starlette app with `to_a2a(root_agent)` + middleware."""
    agent_card_path = os.path.abspath("data/agent.json")
    card = _build_agent_card()
    try:
        os.makedirs(os.path.dirname(agent_card_path), exist_ok=True)
        with open(agent_card_path, "w") as f:
            json.dump(card, f, indent=4)
        logger.info("Agent Card written to %s", agent_card_path)
    except Exception as e:
        logger.error("Failed to write Agent Card: %s", e)

    port = int(os.environ.get("A2A_PORT", 8000))
    starlette_app = to_a2a(
        root_agent,
        host="0.0.0.0",
        port=port,
        agent_card=agent_card_path,
    )

    api_key = os.environ.get("A2A_API_KEY")
    if not api_key:
        logger.error(
            "CRITICAL: A2A_API_KEY missing. Generating a random one for this run; "
            "persist it via setup wizard or /init for stable operation."
        )
        api_key = secrets.token_urlsafe(32)
    starlette_app.add_middleware(A2AApiKeyMiddleware, api_key=api_key)
    return starlette_app


def refresh_agent_card() -> None:
    """Re-write data/agent.json after env (e.g. A2A_BASE_URL) has updated."""
    agent_card_path = os.path.abspath("data/agent.json")
    try:
        os.makedirs(os.path.dirname(agent_card_path), exist_ok=True)
        with open(agent_card_path, "w") as f:
            json.dump(_build_agent_card(), f, indent=4)
        logger.info("Agent Card refreshed at %s", agent_card_path)
    except Exception as e:
        logger.error("Failed to refresh Agent Card: %s", e)


# Eager build at module import — preserves the legacy `from app.a2a_server
# import a2a_app` shape used by run_bot.py.
try:
    a2a_app = build_a2a_app()
except Exception as e:
    logger.error("Failed to build A2A app at import time: %s", e)
    a2a_app = None
