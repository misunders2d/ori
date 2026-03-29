import os
import json
import logging
from fastapi import FastAPI
from starlette.responses import JSONResponse
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from app.sub_agents.coordinator_agent import root_agent

logger = logging.getLogger(__name__)

def create_a2a_app():
    """
    Initializes and configures the A2A Server for this Ori instance.
    This server handles JSON-RPC requests from other agents using the 
    Google ADK Agent-to-Agent protocol.
    """
    # 1. Initialize the base A2A Starlette app from Google ADK
    base_a2a_app = to_a2a(root_agent)

    # 2. Create a FastAPI app to serve standard discovery endpoints
    # FastAPI provides a better interface for adding decorators and handling JSON
    main_app = FastAPI(title="Ori A2A Server")

    # 3. Expose the Agent Card at the standard discovery endpoint
    @main_app.get("/.well-known/agent.json")
    async def get_agent_card():
        """Serves the identity card of this Ori instance for discovery."""
        try:
            # Check for the generated identity card
            if os.path.exists("agent.json"):
                with open("agent.json", "r") as f:
                    return json.load(f)
            
            # Fallback identity if not generated yet
            bot_name = os.environ.get("BOT_NAME", "Ori")
            return {
                "name": bot_name,
                "description": "An autonomous self-evolving digital organism.",
                "status": "identity_pending",
                "message": "Use the 'get_agent_identity' tool to generate the full card."
            }
        except Exception as e:
            logger.error(f"Error serving agent card: {e}")
            return JSONResponse(status_code=500, content={"error": str(e)})

    # 4. Health check for the A2A server
    @main_app.get("/health")
    async def health():
        return {"status": "alive", "agent": os.environ.get("BOT_NAME", "Ori")}

    # 5. Mount the base A2A app at the root
    # Any requests not matched by our specific FastAPI routes above 
    # will be passed to the ADK A2A logic (which handles JSON-RPC POST / etc.)
    main_app.mount("/", base_a2a_app)

    return main_app

# The ASGI application instance
a2a_app = create_a2a_app()
