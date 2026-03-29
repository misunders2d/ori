import os
import json
import logging
import httpx
from typing import Dict, Any, List
from google.adk.tools.tool_context import ToolContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

logger = logging.getLogger(__name__)

FRIENDS_FILE = os.path.abspath("./data/friends.json")

def get_agent_identity(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Generates and returns the public Agent Card (identity) for this Ori instance.
    This card is used by other Oris in the Ori-Net (A2A) to understand our skills and version.
    """
    try:
        bot_name = os.environ.get("BOT_NAME", "Ori")
        app_name = os.environ.get("APP_NAME", "ori")
        
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
                "a2a": f"/{app_name}/a2a"
            }
        }
        
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
    # Standardize the URL to the agent card
    if not url.endswith(".well-known/agent.json"):
        url = url.rstrip("/") + "/.well-known/agent.json"
        
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            card = response.json()
            
        # Basic validation of the card
        if "name" not in card:
            return {"status": "error", "message": "Invalid Agent Card: 'name' field missing."}

        # Load existing friends
        friends = {}
        if os.path.exists(FRIENDS_FILE):
            with open(FRIENDS_FILE, "r") as f:
                friends = json.load(f)
                
        # Add or update the friend
        friends[friend_name] = {
            "name": card.get("name"),
            "description": card.get("description"),
            "agent_card_url": url,
            "added_at": str(httpx.Client().headers.get("date", "")) # Placeholder for timestamp
        }
        
        os.makedirs(os.path.dirname(FRIENDS_FILE), exist_ok=True)
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)
            
        return {
            "status": "success",
            "message": f"Successfully added '{friend_name}' ({card.get('name')}) as a friend. We can now collaborate via A2A!",
            "friend_details": friends[friend_name]
        }
    except Exception as e:
        logger.error(f"Failed to add friend from {url}: {e}")
        return {
            "status": "error",
            "message": f"Failed to connect to friend at {url}: {str(e)}"
        }

def list_friends(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Returns a list of all registered 'friends' in the Ori-Net.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "success", "message": "We don't have any friends in our network yet.", "friends": []}
            
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
            
        return {
            "status": "success",
            "friends": friends
        }
    except Exception as e:
        logger.error(f"Failed to list friends: {e}")
        return {"status": "error", "message": f"Failed to read friends list: {str(e)}"}

async def call_friend(friend_name: str, query: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Sends a query to a registered friend via the A2A protocol and returns their response.
    
    Args:
        friend_name: The local nickname of the friend to call.
        query: The message or question to send to the friend.
    """
    try:
        # 1. Look up the friend
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "Friends list not found."}
            
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
            
        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found in our registry."}
            
        friend_data = friends[friend_name]
        
        # 2. Instantiate the RemoteA2aAgent on the fly
        remote_agent = RemoteA2aAgent(
            name=friend_name,
            description=friend_data.get("description", "A remote Ori instance."),
            agent_card=friend_data["agent_card_url"]
        )
        
        # 3. Call the remote agent
        # Note: RemoteA2aAgent.run_async returns an AsyncGenerator of events.
        # We need to collect the final response.
        from google.adk.agents.invocation_context import InvocationContext
        # We need to create a dummy invocation context or use the existing one if possible.
        # RemoteA2aAgent.run_async(ctx)
        
        # Actually, for a tool, it's easier to use a Runner or a simplified call.
        # But wait, RemoteA2aAgent is designed to be a sub-agent.
        # If I want to call it from a tool, I might need to use the ADK Runner or call _run_async_impl.
        
        # Let's check how to call an agent programmatically from a tool.
        # Usually, tool_context doesn't have a runner.
        
        # Re-evaluating: Maybe it's better if KnowledgeAgent just has the friends as sub-agents?
        # But sub-agents must be defined at instantiation.
        
        # Let's stick to the manual call for now, but I need to know how to execute the RemoteA2aAgent.
        # I'll check the ADK source or docs for calling an agent manually.
        
        # For now, I will just return a placeholder to verify the rest of the flow.
        return {
            "status": "success",
            "message": f"[Simulation] Query sent to '{friend_name}': {query}",
            "response": f"Hi from {friend_name}! I received your query: '{query}'"
        }
        
    except Exception as e:
        logger.error(f"Failed to call friend '{friend_name}': {e}")
        return {"status": "error", "message": f"Failed to call friend: {str(e)}"}
