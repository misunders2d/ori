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
        if path.startswith("/oauth/") and path.endswith("/callback") and request.method == "GET":
            return await _handle_oauth_callback(request)

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

    Looks up the provider in `app.integrations.REGISTRY`, exchanges the
    authorization code for tokens, and stores them via OriCredentialService.
    """
    try:
        path_parts = request.url.path.strip("/").split("/")
        if len(path_parts) < 3:
            return JSONResponse({"status": "error", "message": "Malformed callback path"}, status_code=400)
        provider_name = path_parts[1]
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code:
            err = request.query_params.get("error", "unknown")
            return JSONResponse(
                {"status": "error", "message": f"Authorization rejected: {err}"},
                status_code=400,
            )
        from app.integrations import REGISTRY
        provider = REGISTRY.get(provider_name)
        if provider is None:
            return JSONResponse(
                {"status": "error", "message": f"Unknown OAuth provider: {provider_name}"},
                status_code=404,
            )
        cred = await provider.exchange_code(code)
        # Persist via OriCredentialService — the service uses
        # callback_context to derive user_id, which we don't have here. For
        # the MVP we store under a deterministic key: the caller's session
        # state was tagged with the same `state_token` returned at flow
        # start, so we look up the user/session that originated this
        # request from a small in-memory pending map.
        binding = _consume_pending_oauth(state)
        if binding is None:
            logger.warning("OAuth callback: no pending state token %s", state)
            return JSONResponse(
                {"status": "error", "message": "OAuth state token not recognized."},
                status_code=400,
            )
        user_id = binding.get("user_id", "_global")
        from deploy import vault
        vault_key = f"OAUTH:{provider.name}:{user_id}"
        vault.set(vault_key, cred.model_dump_json(exclude_none=True))
        logger.info("OAuth callback: saved credential under %s", vault_key)
        return JSONResponse({
            "status": "success",
            "message": f"{provider.name} connected. You can close this tab and return to the chat.",
        })
    except Exception as e:
        logger.exception("OAuth callback handler failed")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


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
