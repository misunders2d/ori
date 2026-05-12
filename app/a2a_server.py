import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, FileResponse, Response

# Manual A2A wiring (replaces `to_a2a`) so we can plug a custom
# `gen_ai_part_converter` that strips the `__contract_file:` display_name
# marker from FileParts on the wire. The marker is load-bearing on the
# server-internal genai side (extract_agent_response uses it to dedupe
# Slack/Telegram attachments against the function_response.file_path
# branch). Without the strip, remote A2A peers (Streamlit, other Ori
# instances) see filenames like `__contract_file:/abs/.../chart.png`.
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import AgentCard, FilePart as A2AFilePart, FileWithBytes
from google.adk.a2a.converters.part_converter import convert_genai_part_to_a2a_part
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from app.callbacks.guardrails.attachments import _FILE_ATTACHMENT_MARKER
from app.sub_agents.coordinator_agent import root_agent

logger = logging.getLogger(__name__)

# Discovery paths that remain publicly accessible (no auth required)
_PUBLIC_PATHS = {"/.well-known/agent.json", "/.well-known/agent-card.json"}

# OAuth callback path — intercepted by the middleware itself (never reaches the agent).
_OAUTH_CALLBACK_PATH = "/oauth/google/callback"

DNA_EXPORTS_DIR = os.path.abspath("data/dna_exports")

# Phase 4 hardening — inbound caps. Content-Length above this is rejected
# at the middleware before any body is buffered, so an attacker can't
# OOM the bot by streaming a multi-GB blob.
_INBOUND_MAX_BYTES = int(os.environ.get("A2A_INBOUND_MAX_BYTES", str(20 * 1024 * 1024)))


class A2AApiKeyMiddleware(BaseHTTPMiddleware):
    """Enforces API key authentication on non-discovery A2A endpoints."""

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        # Inbound size cap — reject oversized payloads before buffering
        # the body. `Content-Length` may be missing on streamed requests;
        # in that case we trust the downstream handler to apply its own
        # limit (ADK + Starlette both have defaults).
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                cl = int(content_length)
            except ValueError:
                cl = 0
            if cl > _INBOUND_MAX_BYTES:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32004,
                            "message": (
                                f"Payload too large: {cl} bytes exceeds "
                                f"A2A_INBOUND_MAX_BYTES={_INBOUND_MAX_BYTES}."
                            ),
                        },
                        "id": None,
                    },
                    status_code=413,
                )

        # OAuth callback — handled here, no API key, never reaches the agent
        if request.url.path == _OAUTH_CALLBACK_PATH:
            return await _handle_oauth_callback(request)

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


async def _handle_oauth_callback(request):
    """Handle Google OAuth redirect: exchange the code for tokens, persist per-user.

    URL shape: /oauth/google/callback?code=...&state=...  (or ?error=access_denied&state=...)
    """
    from starlette.responses import HTMLResponse

    params = request.query_params
    state = params.get("state", "")
    code = params.get("code", "")
    error = params.get("error", "")

    if error:
        return HTMLResponse(_oauth_page("Authorization denied", f"Google returned: {error}"), status_code=400)

    if not state or not code:
        return HTMLResponse(_oauth_page("Invalid callback", "Missing state or code parameter."), status_code=400)

    from app.tools.google_oauth import web_flow
    from app.tools.google_oauth.token_store import save_token, save_user_mapping

    result = await web_flow.exchange_code(state, code)
    if result.get("status") != "success":
        return HTMLResponse(_oauth_page("Authorization failed", result.get("message", "Unknown error")), status_code=400)

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
        # Map platform user_id → email so per-user tools can find the token
        if user_id and user_id != email:
            save_user_mapping(user_id, email)
        logger.info("OAuth connect complete for %s (user_id=%s)", email, user_id)
    except Exception as e:
        logger.error("Failed to persist OAuth token: %s", e)
        return HTMLResponse(_oauth_page("Storage failed", str(e)), status_code=500)

    return HTMLResponse(_oauth_page(
        "Connected!",
        f"Google account <strong>{email}</strong> is now connected. You can close this tab and return to the chat.",
    ))


def _oauth_page(title: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:80px auto;padding:0 20px;color:#222;}"
        "h1{font-size:24px;margin-bottom:12px;}p{line-height:1.5;color:#444;}</style>"
        f"</head><body><h1>{title}</h1><p>{body_html}</p></body></html>"
    )


def _validate_peer_url(url: str) -> tuple[bool, str]:
    """SSRF guard: accept only http/https URLs to public hosts.

    Phase 4 §4.3. Without this, an attacker holding the api_key could
    point our friend registry at `http://localhost:6379` or
    `http://169.254.169.254/latest/meta-data/` and use subsequent
    outbound calls to probe internal services.

    Set `A2A_ALLOW_LAN=true` to opt in to private / loopback ranges
    (useful for local dev). Returns (is_safe, reason_if_not).
    """
    if not url:
        return False, "URL is empty."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed (http/https only)."
    host = parsed.hostname or ""
    if not host:
        return False, "no host in URL."
    allow_lan = os.environ.get("A2A_ALLOW_LAN", "").lower() in ("1", "true", "yes")
    if allow_lan:
        return True, ""
    # If hostname is an IP literal, reject loopback / link-local / private ranges.
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_multicast:
            return False, f"host '{host}' is not a public address (set A2A_ALLOW_LAN=true to override)."
        return True, ""
    except ValueError:
        # Not an IP — DNS name. Reject obvious internal-looking hosts.
        lowered = host.lower()
        if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local") or lowered.endswith(".internal"):
            return False, f"host '{host}' is not a public address (set A2A_ALLOW_LAN=true to override)."
        return True, ""


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

        # Phase 4 SSRF guard — reject loopback / private / non-http(s) URLs.
        is_safe, reason = _validate_peer_url(new_url)
        if not is_safe:
            logger.warning("Address update from '%s' rejected: %s", sender_name, reason)
            return JSONResponse(
                {"status": "rejected", "message": f"new_base_url rejected: {reason}"},
                status_code=400,
            )

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


def _ori_gen_ai_part_converter(part):
    """A2A-side wrapper around ADK's default part converter that strips
    the ``__contract_file:`` marker from inline_data display_name on the
    way out. The marker lives on the server-internal genai Part so
    ``extract_agent_response`` can dedupe a captured file against the
    function_response.file_path branch (Slack/Telegram path). Remote A2A
    peers don't see that branch, only the FilePart, and the marker would
    leak into the user-visible filename — so we replace it with the
    basename here.
    """
    a2a_part = convert_genai_part_to_a2a_part(part)
    if a2a_part is None:
        return a2a_part
    root = getattr(a2a_part, "root", None)
    if isinstance(root, A2AFilePart) and isinstance(root.file, FileWithBytes):
        name = root.file.name or ""
        if name.startswith(_FILE_ATTACHMENT_MARKER):
            stripped = name[len(_FILE_ATTACHMENT_MARKER):]
            root.file.name = os.path.basename(stripped) or "attachment"
    return a2a_part


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
    logger.info("Building A2A app (host=0.0.0.0, port=%d) with custom part converter...", port)

    try:
        # Load the agent card we just wrote so we can pass an AgentCard
        # instance into A2AStarletteApplication.
        with open(agent_card_path, "r", encoding="utf-8") as f:
            final_card = AgentCard(**json.load(f))

        async def _create_runner() -> Runner:
            # Mirrors `to_a2a`'s default — in-memory services. Ori's
            # persistent sessions live elsewhere (Slack/Telegram poller);
            # the A2A peer surface intentionally stays ephemeral so a
            # restart doesn't expose persisted state to external callers.
            return Runner(
                app_name=root_agent.name or "ori",
                agent=root_agent,
                artifact_service=InMemoryArtifactService(),
                session_service=InMemorySessionService(),
                memory_service=InMemoryMemoryService(),
                credential_service=InMemoryCredentialService(),
            )

        executor_config = A2aAgentExecutorConfig(
            gen_ai_part_converter=_ori_gen_ai_part_converter,
        )
        agent_executor = A2aAgentExecutor(
            runner=_create_runner,
            config=executor_config,
        )
        request_handler = DefaultRequestHandler(
            agent_executor=agent_executor,
            task_store=InMemoryTaskStore(),
            push_config_store=InMemoryPushNotificationConfigStore(),
        )

        @asynccontextmanager
        async def _lifespan(app_):
            a2a_inner = A2AStarletteApplication(
                agent_card=final_card,
                http_handler=request_handler,
            )
            a2a_inner.add_routes_to_app(app_)
            yield

        app = Starlette(lifespan=_lifespan)

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
