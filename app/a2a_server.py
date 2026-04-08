import os
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, FileResponse, Response

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from app.sub_agents.coordinator_agent import root_agent

logger = logging.getLogger(__name__)

# Discovery paths that remain publicly accessible (no auth required)
_PUBLIC_PATHS = {"/.well-known/agent.json", "/.well-known/agent-card.json"}

DNA_EXPORTS_DIR = os.path.abspath("data/dna_exports")


class A2AApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforces API key authentication on non-discovery A2A endpoints."""

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        provided = request.headers.get("x-a2a-api-key", "")
        if provided != self.api_key:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32001, "message": "Unauthorized: invalid or missing x-a2a-api-key header"},
                    "id": None,
                },
                status_code=401,
            )

        # Deterministic address update endpoint (no agent involved)
        if request.url.path == "/a2a/address-update" and request.method == "POST":
            return await _handle_address_update(request)

        # Serve DNA archives directly (authenticated, out-of-band transfer)
        if request.url.path.startswith("/dna/"):
            filename = request.url.path[len("/dna/"):]
            # Security: reject path traversal
            if "/" in filename or ".." in filename or not filename.endswith(".tar.gz"):
                return Response("Not found", status_code=404)
            archive_path = os.path.join(DNA_EXPORTS_DIR, filename)
            if os.path.isfile(archive_path):
                return FileResponse(archive_path, media_type="application/gzip", filename=filename)
            return Response("Not found", status_code=404)

        return await call_next(request)


FRIENDS_FILE = os.path.abspath("./data/friends.json")


async def _handle_address_update(request) -> JSONResponse:
    """Deterministic handler: update a friend's URL when they broadcast a new address.

    Expects JSON: {"sender_name": "...", "new_base_url": "https://..."}
    Authenticated via x-a2a-api-key header (already validated by middleware).

    Matching priority:
      1. Sender's API key matches a stored friend key (definitive)
      2. sender_name matches a friend's registry key or card name (case-insensitive)
    """
    try:
        body = await request.json()
        sender_name = body.get("sender_name", "")
        new_url = body.get("new_base_url", "").rstrip("/")

        if not sender_name or not new_url:
            return JSONResponse({"status": "error", "message": "Missing sender_name or new_base_url"}, status_code=400)

        if not os.path.exists(FRIENDS_FILE):
            return JSONResponse({"status": "ignored", "message": "No friends registered"})

        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)

        # Load stored friend keys for matching
        sender_api_key = request.headers.get("x-a2a-api-key", "")
        stored_keys = {}
        keys_file = os.path.abspath("./data/a2a_keys.json")
        if os.path.exists(keys_file):
            try:
                with open(keys_file, "r") as f:
                    stored_keys = json.load(f)
            except Exception:
                pass

        # Match by API key first (most reliable — keys are unique per friend)
        matched_key = None
        if sender_api_key:
            for nickname, key in stored_keys.items():
                if key == sender_api_key and nickname in friends:
                    matched_key = nickname
                    break

        # Fallback: match by name
        if not matched_key:
            for key, data in friends.items():
                if (key.lower() == sender_name.lower()
                        or data.get("name", "").lower() == sender_name.lower()):
                    matched_key = key
                    break

        if not matched_key:
            logger.info("Address update from unknown sender '%s' — ignored", sender_name)
            return JSONResponse({"status": "ignored", "message": f"Unknown sender: {sender_name}"})

        old_url = friends[matched_key].get("base_url", "")
        friends[matched_key]["base_url"] = new_url
        friends[matched_key]["endpoint_url"] = new_url
        from datetime import datetime
        friends[matched_key]["last_address_update"] = datetime.now().isoformat()

        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)

        logger.info("Auto-updated friend '%s' URL: %s → %s", matched_key, old_url, new_url)
        return JSONResponse({"status": "success", "message": f"Updated {matched_key} to {new_url}"})

    except Exception as e:
        logger.error("Address update handler failed: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


def _build_agent_card() -> dict:
    """Build a v1.0-compliant A2A Agent Card from environment configuration."""
    bot_name = os.environ.get("BOT_NAME", "Ori")
    base_url = os.environ.get("A2A_BASE_URL", "http://localhost:8000")
    api_key_set = bool(os.environ.get("A2A_API_KEY"))

    card = {
        "id": os.environ.get("A2A_AGENT_ID", f"ori-{bot_name.lower()}"),
        "name": bot_name,
        "version": "1.0.0",
        "description": "An autonomous self-evolving digital organism.",
        "url": base_url,
        "provider": {
            "organization": os.environ.get("A2A_PROVIDER_NAME", "Ori Project"),
            "url": os.environ.get("A2A_PROVIDER_URL", base_url),
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
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
                "description": "General-purpose conversation, task management, and scheduling.",
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
                "description": "Exchanges sanitized technical improvements (tools/skills) with other Ori instances.",
                "tags": ["a2a", "exchange"],
            },
        ],
    }

    # Declare security scheme if API key is configured
    if api_key_set:
        card["securitySchemes"] = {
            "apiKey": {
                "type": "apiKey",
                "name": "x-a2a-api-key",
                "in": "header",
            }
        }
        card["security"] = [{"apiKey": []}]

    return card


def refresh_agent_card():
    """Rebuild and persist the Agent Card (e.g. after tunnel URL detection)."""
    agent_card_path = os.path.abspath("data/agent.json")
    card = _build_agent_card()
    try:
        with open(agent_card_path, "w") as f:
            json.dump(card, f, indent=4)
        logger.info("Agent Card refreshed with URL: %s", card.get("url"))
    except Exception as e:
        logger.error("Failed to refresh Agent Card: %s", e)


def create_a2a_app():
    """Initialize the A2A server with a v1.0-compliant Agent Card and optional API key auth."""
    logger.info("Initializing A2A Server application...")

    # Build and persist the Agent Card (single source of truth)
    agent_card_path = os.path.abspath("data/agent.json")
    card = _build_agent_card()

    try:
        os.makedirs(os.path.dirname(agent_card_path), exist_ok=True)
        with open(agent_card_path, "w") as f:
            json.dump(card, f, indent=4)
        logger.info("Agent Card (v1.0) written to %s", agent_card_path)
    except Exception as e:
        logger.error("Failed to write Agent Card: %s", e)

    port = int(os.environ.get("A2A_PORT", 8000))
    logger.info("Wrapping root_agent with to_a2a (host=0.0.0.0, port=%d)...", port)

    try:
        app = to_a2a(
            agent=root_agent,
            host="0.0.0.0",
            port=port,
            agent_card=agent_card_path,
        )

        # Layer API key middleware if configured
        api_key = os.environ.get("A2A_API_KEY")
        if not api_key:
            logger.error(
                "CRITICAL: A2A_API_KEY is missing from your .env file! "
                "The A2A server will NOT start. This key is strictly required "
                "to secure your agent from unauthorized internet access and quota drain."
            )
            return None
            
        app = A2AApiKeyMiddleware(app, api_key)
        logger.info("A2A API key authentication strictly enforced.")

        logger.info("A2A Server application initialized successfully.")
        return app
    except Exception as e:
        logger.error("Failed to create A2A app: %s", e)
        # Don't crash the daemon if A2A fails (e.g. port already bound)
        return None


# The ASGI application instance
a2a_app = create_a2a_app()
