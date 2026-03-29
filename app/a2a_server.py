import os
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
    # Path to the Agent Card (Digital Business Card)
    # This file is generated/updated by the 'get_agent_identity' tool.
    agent_card_path = os.path.abspath("agent.json")
    
    # If the card doesn't exist yet, we'll still start.
    # The to_a2a function will serve a default or empty card until one is provided.
    if not os.path.exists(agent_card_path):
        logger.warning(f"Agent card not found at {agent_card_path}. Use 'get_agent_identity' to generate it.")

    # Native ADK A2A Server initialization.
    # This automatically handles:
    # - JSON-RPC execution at /
    # - Discovery at /.well-known/agent-card.json (standard ADK path)
    # - Discovery at /.well-known/agent.json (if symlinked or handled)
    # We prioritize the native ADK implementation as requested.
    logger.info("Wrapping root_agent with to_a2a wrapper...")
    try:
        app = to_a2a(
            agent=root_agent,
            agent_card=agent_card_path if os.path.exists(agent_card_path) else None
        )
        logger.info("A2A Server application successfully initialized.")
        return app
    except Exception as e:
        logger.error(f"Failed to create A2A app via to_a2a: {e}")
        raise

# The ASGI application instance (Starlette app)
a2a_app = create_a2a_app()
