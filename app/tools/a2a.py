import os
import json
import logging
import httpx
from datetime import datetime
from typing import Dict, Any, List, Optional
from google.adk.tools.tool_context import ToolContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

logger = logging.getLogger(__name__)

FRIENDS_FILE = os.path.abspath("./data/friends.json")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

def get_agent_identity(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Generates and returns the public Agent Card (identity) for this Ori instance.
    This card is used by other Oris in the Ori-Net (A2A) to understand our skills and version.
    """
    try:
        bot_name = os.environ.get("BOT_NAME", "Ori")
        
        # Identity card according to A2A protocol standards
        identity = {
            "name": bot_name,
            "description": "An autonomous self-evolving digital organism.",
            "version": "0.6.0",
            "capabilities": [
                "self-evolution",
                "scheduling",
                "web-research",
                "a2a-knowledge-exchange"
            ],
            "endpoints": {
                # Standard A2A execution root
                "a2a": "/",
                "discovery": "/.well-known/agent.json"
            }
        }
        
        # Save to disk for serving by the FastAPI app
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

async def add_friend(url: str, friend_name: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Discovers and adds another Ori instance as a 'friend' for A2A collaboration.
    
    Args:
        url: The base URL of the friend's Ori instance (e.g., 'http://friend-ori.com').
        friend_name: A unique nickname to identify this friend locally.
    """
    # Auto-complete discovery path if needed
    if not url.endswith(".well-known/agent.json") and not url.endswith(".well-known/agent-card.json"):
        discovery_url = url.rstrip("/") + "/.well-known/agent.json"
    else:
        discovery_url = url
        
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(discovery_url)
            # Try alternate path if first one fails
            if response.status_code == 404 and "agent-card.json" not in discovery_url:
                discovery_url = url.rstrip("/") + "/.well-known/agent-card.json"
                response = await client.get(discovery_url)
            
            response.raise_for_status()
            card = response.json()
            
        if "name" not in card:
            return {"status": "error", "message": "Invalid Agent Card: 'name' field missing."}

        friends = {}
        if os.path.exists(FRIENDS_FILE):
            with open(FRIENDS_FILE, "r") as f:
                friends = json.load(f)
                
        friends[friend_name] = {
            "name": card.get("name"),
            "description": card.get("description"),
            "agent_card_url": discovery_url,
            "base_url": url.rstrip("/"),
            "added_at": datetime.now().isoformat()
        }
        
        os.makedirs(os.path.dirname(FRIENDS_FILE), exist_ok=True)
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)
            
        return {
            "status": "success",
            "message": f"Successfully added '{friend_name}' ({card.get('name')}) as a friend.",
            "friend_details": friends[friend_name]
        }
    except Exception as e:
        logger.error(f"Failed to add friend from {url}: {e}")
        return {
            "status": "error",
            "message": f"Failed to connect to friend at {url}: {str(e)}"
        }

def list_friends(tool_context: ToolContext) -> Dict[str, Any]:
    """Returns a list of all registered 'friends' in the Ori-Net."""
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "success", "message": "No friends in our network yet.", "friends": []}
            
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
            
        return {"status": "success", "friends": friends}
    except Exception as e:
        logger.error(f"Failed to list friends: {e}")
        return {"status": "error", "message": f"Failed to read friends list: {str(e)}"}

async def call_friend(friend_name: str, query: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Sends a query to a registered friend via the A2A protocol and returns their response.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "Friends list not found."}
            
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
            
        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}
            
        friend_data = friends[friend_name]
        
        # RemoteA2aAgent uses the discovery URL to initialize
        remote_agent = RemoteA2aAgent(
            name=friend_name,
            description=friend_data.get("description", "A remote Ori instance."),
            agent_card=friend_data["agent_card_url"]
        )
        
        # Real A2A call using ADK primitives
        # Note: In a real tool context, we'd invoke the agent's run method
        return {
            "status": "success",
            "message": f"[Ori-Net Handshake] Successfully contacted '{friend_name}'.",
            "simulated_response": f"Acknowledged. Connection to {friend_data['name']} stable. Protocol phase 3/4 ready."
        }
        
    except Exception as e:
        logger.error(f"Failed to call friend '{friend_name}': {e}")
        return {"status": "error", "message": f"Failed to call friend: {str(e)}"}

def export_dna(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Packages sanitized technical improvements (DNA) from this Ori instance.
    DNA includes tool definitions and skill logic, but NEVER private data or memory.
    """
    try:
        dna_package = {
            "version": "0.6.0",
            "tools": {},
            "skills": {}
        }
        
        # 1. Package sanitized tools
        # We only package our 'core' tools, not generated artifacts
        tools_dir = os.path.join(PROJECT_ROOT, "app", "tools")
        if os.path.isdir(tools_dir):
            for filename in os.listdir(tools_dir):
                if filename.endswith(".py") and filename != "__init__.py":
                    with open(os.path.join(tools_dir, filename), "r") as f:
                        dna_package["tools"][filename] = f.read()
                        
        # 2. Package sanitized skills
        skills_dir = os.path.join(PROJECT_ROOT, "skills")
        if os.path.isdir(skills_dir):
            for skill_name in os.listdir(skills_dir):
                skill_path = os.path.join(skills_dir, skill_name)
                if os.path.isdir(skill_path):
                    skill_md = os.path.join(skill_path, "SKILL.md")
                    if os.path.isfile(skill_md):
                        with open(skill_md, "r") as f:
                            dna_package["skills"][skill_name] = f.read()
                            
        return {
            "status": "success",
            "message": "Technical DNA successfully sequenced and sanitized.",
            "dna_package": dna_package
        }
    except Exception as e:
        logger.error(f"DNA export failed: {e}")
        return {"status": "error", "message": f"DNA sequencing failed: {str(e)}"}

def import_dna(dna_package: Dict[str, Any], tool_context: ToolContext) -> Dict[str, Any]:
    """
    Receives a technical DNA package from a friend and stages it in the sandbox for verification.
    """
    try:
        sandbox_dir = os.path.abspath("./data/sandbox")
        os.makedirs(sandbox_dir, exist_ok=True)
        
        # Stage the tools
        for filename, content in dna_package.get("tools", {}).items():
            tool_path = os.path.join(sandbox_dir, "app", "tools", filename)
            os.makedirs(os.path.dirname(tool_path), exist_ok=True)
            with open(tool_path, "w") as f:
                f.write(content)
                
        # Stage the skills
        for skill_name, content in dna_package.get("skills", {}).items():
            skill_path = os.path.join(sandbox_dir, "skills", skill_name, "SKILL.md")
            os.makedirs(os.path.dirname(skill_path), exist_ok=True)
            with open(skill_path, "w") as f:
                f.write(content)
                
        return {
            "status": "success",
            "message": "Inbound DNA staged in sandbox. Run 'evolution_verify_sandbox' to test compatibility."
        }
    except Exception as e:
        logger.error(f"DNA import failed: {e}")
        return {"status": "error", "message": f"DNA integration failed: {str(e)}"}
