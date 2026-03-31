import os
import json
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from app.sub_agents.coordinator_agent import root_agent

logger = logging.getLogger(__name__)

# Discovery paths that remain publicly accessible (no auth required)
_PUBLIC_PATHS = {"/.well-known/agent.json", "/.well-known/agent-card.json"}


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
        return await call_next(request)


def _build_agent_card() -> dict:
    """Build a v1.0-compliant A2A Agent Card from environment configuration."""
    bot_name = os.environ.get("BOT_NAME", "Ori")
    base_url = os.environ.get("A2A_BASE_URL", "http://localhost:8000")
    api_key_set = bool(os.environ.get("A2A_API_KEY"))

    card = {
        "id": os.environ.get("A2A_AGENT_ID", f"ori-{bot_name.lower()}"),
        "name": bot_name,
        "version": "0.7.0",
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
