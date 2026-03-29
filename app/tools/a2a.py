import os
import json
import logging
from typing import Dict, Any
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

def get_agent_identity(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Generates and returns the public Agent Card (identity) for this Ori instance.
    This card is used by other Oris in the Ori-Net (A2A) to understand our skills and version.
    """
    try:
        # 1. Read project metadata
        # We can use the environment variables or pyproject.toml
        # For simplicity in this tool, we will use the ENV first
        bot_name = os.environ.get("BOT_NAME", "Ori")
        app_name = os.environ.get("APP_NAME", "ori")
        
        # In a real scenario, we'd read this from pyproject.toml
        # But for this tool, we'll use a standard structure
        identity = {
            "name": bot_name,
            "description": "An autonomous self-evolving digital organism.",
            "version": "0.6.0", # Hardcoded for now, should be dynamic in Phase 2
            "capabilities": [
                "self-evolution",
                "scheduling",
                "web-research",
                "a2a-knowledge-exchange"
            ],
            "endpoints": {
                "a2a": f"/{app_name}/a2a"
            }
        }
        
        # Write to root for static hosting in Phase 2
        with open("agent.json", "w") as f:
            json.dump(identity, f, indent=4)
            
        return {
            "status": "success",
            "message": f"Agent Card for {bot_name} generated successfully. This is our 'Digital Business Card'.",
            "identity": identity
        }
    except Exception as e:
        logger.error(f"Failed to generate Agent Card: {e}")
        return {
            "status": "error",
            "message": f"Failed to generate Agent Card: {str(e)}"
        }
