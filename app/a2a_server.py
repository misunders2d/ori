import os
import json
import logging
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from app.sub_agents.coordinator_agent import root_agent

logger = logging.getLogger(__name__)

def create_a2a_app():
    """
    Initializes and configures the A2A Server for this Ori instance.
    Uses the native Google ADK to_a2a wrapper for maximum protocol compliance.
    """
    logger.info("Initializing A2A Server application...")
    
    # Path to the Agent Card (Digital Business Card) in the persistent data directory
    agent_card_path = os.path.abspath("data/agent.json")
    
    # Ensure Agent Card exists before starting
    if not os.path.exists(agent_card_path):
        logger.info(f"Bootstrapping default Agent Card at {agent_card_path}...")
        bot_name = os.environ.get("BOT_NAME", "Ori")
        default_identity = {
            "name": bot_name,
            "description": "An autonomous self-evolving digital organism.",
            "version": "0.6.0",
            "capabilities": ["self-evolution", "scheduling", "web-research", "a2a-knowledge-exchange"],
            "endpoints": {
                "a2a": "/",
                "discovery": ["/.well-known/agent.json", "/.well-known/agent-card.json"]
            }
        }
        try:
            os.makedirs(os.path.dirname(agent_card_path), exist_ok=True)
            with open(agent_card_path, "w") as f:
                json.dump(default_identity, f, indent=4)
            logger.info("Default Agent Card generated successfully.")
        except Exception as e:
            logger.error(f"Failed to bootstrap Agent Card: {e}")

    # Native ADK A2A Server initialization.
    port = int(os.environ.get("A2A_PORT", 8000))
    logger.info(f"Wrapping root_agent with to_a2a wrapper (host=0.0.0.0, port={port})...")
    
    try:
        app = to_a2a(
            agent=root_agent,
            host="0.0.0.0",
            port=port,
            agent_card=agent_card_path
        )
        logger.info("A2A Server application successfully initialized.")
        return app
    except Exception as e:
        logger.error(f"Failed to create A2A app via to_a2a: {e}")
        raise

# The ASGI application instance (Starlette app)
a2a_app = create_a2a_app()
