import os
import json
import logging
import uuid
import httpx
from datetime import datetime
from typing import Dict, Any, Optional
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

FRIENDS_FILE = os.path.abspath("./data/friends.json")
AGENT_CARD_PATH = os.path.abspath("./data/agent.json")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Agent Card (read-only — built once at startup by a2a_server.py)
# ---------------------------------------------------------------------------

def get_agent_identity(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Returns this agent's public A2A v1.0 Agent Card.
    The card is generated at startup by the A2A server; this tool only reads it.
    """
    try:
        if not os.path.exists(AGENT_CARD_PATH):
            return {
                "status": "error",
                "message": "Agent Card not found at data/agent.json. The A2A server may not have started yet.",
            }
        with open(AGENT_CARD_PATH, "r") as f:
            card = json.load(f)
        return {"status": "success", "identity": card}
    except Exception as e:
        logger.error("Failed to read Agent Card: %s", e)
        return {"status": "error", "message": f"Failed to read Agent Card: {e}"}


# ---------------------------------------------------------------------------
# Discovery & Friendship
# ---------------------------------------------------------------------------

async def _discover_agent_card(base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a remote agent's card via standard .well-known discovery paths."""
    base_url = base_url.rstrip("/")
    discovery_paths = ["/.well-known/agent.json", "/.well-known/agent-card.json"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        for path in discovery_paths:
            try:
                resp = await client.get(base_url + path)
                if resp.status_code == 200:
                    card = resp.json()
                    # Require at least a name (minimum validity check)
                    if "name" not in card:
                        continue
                    # Tolerate pre-v1.0 cards that lack endpoints array
                    if "endpoints" not in card:
                        card["endpoints"] = [{"type": "json-rpc", "url": base_url}]
                    return card
            except Exception as e:
                logger.debug("Discovery failed at %s%s: %s", base_url, path, e)
    return None


async def add_friend(url: str, friend_name: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Discovers and registers another A2A-compliant agent as a friend for ongoing collaboration.

    Args:
        url: The base URL of the remote agent (e.g., 'http://agent.example.com').
        friend_name: A unique local nickname for this friend.
    """
    card = await _discover_agent_card(url)
    if not card:
        return {
            "status": "error",
            "message": f"No valid Agent Card found at {url}. Is the remote agent online and A2A-compliant?",
        }

    # Extract the A2A endpoint URL from the card
    base_url = url.rstrip("/")
    endpoint_url = base_url
    for ep in card.get("endpoints", []):
        if ep.get("type") in ("json-rpc", "http+json"):
            endpoint_url = ep["url"]
            break

    # Detect if the remote agent requires auth
    required_security = card.get("security", [])

    try:
        friends = {}
        if os.path.exists(FRIENDS_FILE):
            with open(FRIENDS_FILE, "r") as f:
                friends = json.load(f)

        friends[friend_name] = {
            "name": card.get("name"),
            "description": card.get("description", ""),
            "base_url": base_url,
            "endpoint_url": endpoint_url,
            "card": card,
            "required_security": required_security,
            "added_at": datetime.now().isoformat(),
        }

        os.makedirs(os.path.dirname(FRIENDS_FILE), exist_ok=True)
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)

        security_note = ""
        if required_security:
            security_note = (
                " Note: this agent declares security requirements. "
                "You may need to configure an API key for authenticated calls."
            )

        return {
            "status": "success",
            "message": f"Added '{friend_name}' ({card.get('name')}) as a friend.{security_note}",
            "friend": {
                "name": card.get("name"),
                "description": card.get("description", ""),
                "endpoint_url": endpoint_url,
                "capabilities": card.get("capabilities", {}),
                "skills": [s.get("name") for s in card.get("skills", [])],
            },
        }
    except Exception as e:
        logger.error("Failed to save friend %s: %s", friend_name, e)
        return {"status": "error", "message": f"Discovery succeeded but save failed: {e}"}


def list_friends(tool_context: ToolContext) -> Dict[str, Any]:
    """Returns all registered friends in the network with their capabilities."""
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "success", "message": "No friends registered yet.", "friends": {}}
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
        # Return a summary (not the full stored cards)
        summary = {}
        for nickname, data in friends.items():
            summary[nickname] = {
                "name": data.get("name"),
                "description": data.get("description", ""),
                "endpoint_url": data.get("endpoint_url"),
                "added_at": data.get("added_at"),
            }
        return {"status": "success", "friends": summary}
    except Exception as e:
        logger.error("Failed to list friends: %s", e)
        return {"status": "error", "message": f"Failed to read friends list: {e}"}


# ---------------------------------------------------------------------------
# A2A Communication (JSON-RPC client)
# ---------------------------------------------------------------------------

async def _send_a2a_message(
    endpoint_url: str,
    message_text: str,
    task_id: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a JSON-RPC message/send request to a remote A2A agent."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-a2a-api-key"] = api_key

    params: Dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"text": message_text}],
        },
    }
    if task_id:
        params["message"]["taskId"] = task_id

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": params,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(endpoint_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _extract_response_text(task: Dict[str, Any]) -> str:
    """Extract human-readable text from an A2A Task response object."""
    texts = []

    # Check artifacts first (generated outputs)
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if "text" in part:
                texts.append(part["text"])

    # Fall back to agent messages in history
    if not texts:
        for msg in task.get("messages", []):
            if msg.get("role") == "agent":
                for part in msg.get("parts", []):
                    if "text" in part:
                        texts.append(part["text"])

    # Last resort: status message
    if not texts:
        status = task.get("status", {})
        if isinstance(status, dict):
            status_msg = status.get("message")
            if status_msg:
                texts.append(status_msg)

    return "\n".join(texts) if texts else "(no text in response)"


async def call_friend(friend_name: str, message: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Sends a message to a registered friend via the A2A protocol and returns their response.

    Args:
        friend_name: The local nickname of the friend to contact.
        message: The message to send to the friend.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "No friends registered yet. Use add_friend first."}

        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)

        if friend_name not in friends:
            available = ", ".join(friends.keys()) if friends else "none"
            return {
                "status": "error",
                "message": f"Friend '{friend_name}' not found. Available friends: {available}",
            }

        friend = friends[friend_name]
        endpoint_url = friend.get("endpoint_url", friend.get("base_url"))
        api_key = friend.get("api_key")  # Optional stored key for authenticated friends

        result = await _send_a2a_message(endpoint_url, message, api_key=api_key)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        response_text = _extract_response_text(task)

        return {
            "status": "success",
            "friend": friend_name,
            "remote_agent": friend.get("name"),
            "task_id": task.get("id"),
            "task_state": task.get("status", {}).get("state", "unknown") if isinstance(task.get("status"), dict) else "unknown",
            "response": response_text,
        }
    except httpx.HTTPStatusError as e:
        return {"status": "error", "message": f"HTTP {e.response.status_code} from {friend_name}: {e.response.text[:200]}"}
    except httpx.ConnectError:
        return {"status": "error", "message": f"Could not connect to {friend_name}. Is the remote agent online?"}
    except Exception as e:
        logger.error("Failed to call friend '%s': %s", friend_name, e)
        return {"status": "error", "message": f"Failed to call friend: {e}"}


async def call_agent(url: str, message: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Sends a one-off message to any A2A-compliant agent by URL.
    Discovers the agent's card first, then sends the message via JSON-RPC.
    Use this for agents NOT in the friends list.

    Args:
        url: The base URL of the remote A2A agent (e.g., 'https://agent.example.com').
        message: The message to send to the remote agent.
    """
    try:
        card = await _discover_agent_card(url)
        if not card:
            return {
                "status": "error",
                "message": f"No valid Agent Card found at {url}. Is the remote agent online and A2A-compliant?",
            }

        # Resolve the A2A endpoint from the card
        endpoint_url = url.rstrip("/")
        for ep in card.get("endpoints", []):
            if ep.get("type") in ("json-rpc", "http+json"):
                endpoint_url = ep["url"]
                break

        result = await _send_a2a_message(endpoint_url, message)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        response_text = _extract_response_text(task)

        return {
            "status": "success",
            "agent_name": card.get("name", "unknown"),
            "agent_id": card.get("id", "unknown"),
            "task_id": task.get("id"),
            "task_state": task.get("status", {}).get("state", "unknown") if isinstance(task.get("status"), dict) else "unknown",
            "response": response_text,
        }
    except httpx.HTTPStatusError as e:
        return {"status": "error", "message": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
    except httpx.ConnectError:
        return {"status": "error", "message": f"Could not connect to {url}. Is the remote agent online?"}
    except Exception as e:
        logger.error("Failed to call agent at %s: %s", url, e)
        return {"status": "error", "message": f"Failed to call agent: {e}"}


# ---------------------------------------------------------------------------
# DNA Exchange (Ori-specific extension — not part of A2A v1.0 standard)
# ---------------------------------------------------------------------------

def export_dna(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Packages sanitized technical improvements (DNA) from this Ori instance.
    DNA includes tool definitions and skill logic, but NEVER private data or memory.
    This is an Ori-specific extension, not part of the A2A standard.
    """
    try:
        dna_package = {
            "version": "0.7.0",
            "tools": {},
            "skills": {},
        }

        # Package sanitized tools
        tools_dir = os.path.join(PROJECT_ROOT, "app", "tools")
        if os.path.isdir(tools_dir):
            for filename in os.listdir(tools_dir):
                if filename.endswith(".py") and filename != "__init__.py":
                    with open(os.path.join(tools_dir, filename), "r") as f:
                        dna_package["tools"][filename] = f.read()

        # Package sanitized skills
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
            "dna_package": dna_package,
        }
    except Exception as e:
        logger.error("DNA export failed: %s", e)
        return {"status": "error", "message": f"DNA sequencing failed: {e}"}


def import_dna(dna_package: Dict[str, Any], tool_context: ToolContext) -> Dict[str, Any]:
    """
    Receives a technical DNA package from a friend and stages it in the sandbox for verification.
    This is an Ori-specific extension, not part of the A2A standard.
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
            "message": "Inbound DNA staged in sandbox. Run 'evolution_verify_sandbox' to test compatibility.",
        }
    except Exception as e:
        logger.error("DNA import failed: %s", e)
        return {"status": "error", "message": f"DNA integration failed: {e}"}
