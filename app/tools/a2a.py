import os
import json
import logging
import uuid
import httpx
from datetime import datetime
from typing import Dict, Any, Optional, List
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

FRIENDS_FILE = os.path.abspath("./data/friends.json")
KEYS_FILE = os.path.abspath("./data/a2a_keys.json")
A2A_STATE_FILE = os.path.abspath("./data/a2a_state.json")
AGENT_CARD_PATH = os.path.abspath("./data/agent.json")
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Key Management (Private Helpers)
# ---------------------------------------------------------------------------

def _load_friend_key(friend_name: str) -> Optional[str]:
    """Retrieves a stored API key for a friend from the secure keys file."""
    try:
        if not os.path.exists(KEYS_FILE):
            return None
        with open(KEYS_FILE, "r") as f:
            keys = json.load(f)
        return keys.get(friend_name)
    except Exception as e:
        logger.error("Failed to load keys: %s", e)
        return None


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
    API keys are stored separately and preserved across updates.

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
            "last_discovered_at": datetime.now().isoformat(),
        }

        os.makedirs(os.path.dirname(FRIENDS_FILE), exist_ok=True)
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)

        has_key = bool(_load_friend_key(friend_name))
        security_note = ""
        if required_security and not has_key:
            security_note = (
                " Note: this agent declares security requirements and no key is stored. "
                "You MUST invoke `update_friend_key` now to request the API key securely from the user."
            )

        return {
            "status": "success",
            "message": f"Registered '{friend_name}' ({card.get('name')}) as a friend.{security_note}",
            "friend": {
                "name": card.get("name"),
                "endpoint_url": endpoint_url,
                "capabilities": card.get("capabilities", {}),
            },
        }
    except Exception as e:
        logger.error("Failed to save friend %s: %s", friend_name, e)
        return {"status": "error", "message": f"Discovery succeeded but save failed: {e}"}


def update_friend_key(friend_name: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Initiates a secure capture flow to configure an API key for a registered A2A friend.
    
    API keys are sensitive and stored in data/a2a_keys.json (git-ignored).
    This tool registers an interceptor. You must tell the user to provide the key in their NEXT message.

    Args:
        friend_name: The local nickname of the friend to update.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "No friends registered yet. Use add_friend first."}

        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)

        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}

        from app.secure_config import expect_friend_key
        
        current_state = tool_context.state.to_dict() if tool_context and hasattr(tool_context, "state") else {}
        session_id = current_state.get("session_id") or str(getattr(tool_context.session, "id", "default"))
        
        expect_friend_key(session_id, friend_name)

        return {
            "status": "success",
            "message": (
                f"Secure capture armed for {friend_name}. Tell the user: 'Please reply with the API key "
                f"for {friend_name}. I will intercept and save it securely without logging it in our chat history.'"
            ),
        }
    except Exception as e:
        logger.error("Failed to arm secure capture: %s", e)
        return {"status": "error", "message": f"Failed to arm secure capture: {e}"}


def list_friends(tool_context: ToolContext) -> Dict[str, Any]:
    """Returns all registered friends in the network with their capabilities and key status."""
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "success", "message": "No friends registered yet.", "friends": {}}
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
        summary = {}
        for nickname, data in friends.items():
            has_key = bool(_load_friend_key(nickname))
            summary[nickname] = {
                "name": data.get("name"),
                "base_url": data.get("base_url"),
                "endpoint_url": data.get("endpoint_url"),
                "last_active": data.get("last_discovered_at"),
                "auth_status": "key_configured" if has_key else "key_missing",
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

    message_obj: Dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"text": message_text}],
    }
    
    if task_id:
        message_obj["taskId"] = task_id

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": message_obj,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(endpoint_url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _to_string(val: Any) -> str:
    """Helper to convert any A2A part value to a string."""
    if isinstance(val, str):
        return val
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, separators=(",", ":"))
    return str(val)


def _extract_response_text(task: Dict[str, Any]) -> str:
    """Extract human-readable text from an A2A Task response object."""
    texts = []

    # 1. Look for artifacts (A2A v1.0 standard)
    artifacts = task.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            parts = artifact.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and "text" in part:
                        texts.append(_to_string(part["text"]))

    # 2. Look for explicit messages (legacy/fallback)
    if not texts:
        messages = task.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") == "agent":
                    parts = msg.get("parts")
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and "text" in part:
                                texts.append(_to_string(part["text"]))

    # 3. Look for status message
    if not texts:
        status = task.get("status", {})
        if isinstance(status, dict):
            status_msg = status.get("message")
            if status_msg:
                texts.append(_to_string(status_msg))

    return "\n".join(texts) if texts else "(no text in response)"


async def call_friend(friend_name: str, message: str, tool_context: ToolContext = None) -> Dict[str, Any]:
    """
    Sends a message to a registered friend via the A2A protocol and returns their response.

    Args:
        friend_name: The local nickname of the friend to contact.
        message: The message to send to the friend.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "No friends registered yet."}

        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)

        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}

        friend = friends[friend_name]
        endpoint_url = friend.get("endpoint_url", friend.get("base_url"))
        api_key = _load_friend_key(friend_name)

        result = await _send_a2a_message(endpoint_url, message, api_key=api_key)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        response_text = _extract_response_text(task)

        return {
            "status": "success",
            "friend": friend_name,
            "task_id": task.get("id"),
            "response": response_text,
        }
    except Exception as e:
        logger.error("A2A call to %s failed: %s", friend_name, e)
        return {"status": "error", "message": f"A2A call failed: {e}"}


async def call_agent(url: str, message: str, tool_context: ToolContext, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Sends a one-off message to any A2A-compliant agent by URL.
    Use this for agents NOT in the friends list.

    Args:
        url: The base URL of the remote A2A agent (e.g., 'https://agent.example.com').
        message: The message to send to the remote agent.
        api_key: Optional API key for authentication.
    """
    try:
        card = await _discover_agent_card(url)
        if not card:
            return {"status": "error", "message": "No valid Agent Card found."}

        endpoint_url = url.rstrip("/")
        for ep in card.get("endpoints", []):
            if ep.get("type") in ("json-rpc", "http+json"):
                endpoint_url = ep["url"]
                break

        # If no key provided, check if we have a friend registered for this URL
        if not api_key and os.path.exists(FRIENDS_FILE):
            with open(FRIENDS_FILE, "r") as f:
                friends = json.load(f)
            for nick, data in friends.items():
                if data.get("base_url") == url.rstrip("/") or data.get("endpoint_url") == endpoint_url:
                    api_key = _load_friend_key(nick)
                    break

        result = await _send_a2a_message(endpoint_url, message, api_key=api_key)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        return {
            "status": "success",
            "agent_name": card.get("name", "unknown"),
            "response": _extract_response_text(task),
        }
    except Exception as e:
        logger.error("A2A one-off call to %s failed: %s", url, e)
        return {"status": "error", "message": f"A2A call failed: {e}"}


# ---------------------------------------------------------------------------
# Ori-Net Protocol Extensions: Presence & Address Updates
# ---------------------------------------------------------------------------

async def perform_a2a_broadcast(force: bool = False) -> Dict[str, Any]:
    """
    System helper to broadcast the current A2A_BASE_URL to all friends.
    Stores state in data/a2a_state.json to prevent redundant broadcasts.
    """
    my_url = os.environ.get("A2A_BASE_URL")
    if not my_url:
        logger.debug("A2A broadcast skipped: A2A_BASE_URL not set.")
        return {"status": "skipped", "reason": "A2A_BASE_URL not set"}

    # Check if address actually changed
    last_url = None
    if os.path.exists(A2A_STATE_FILE):
        try:
            with open(A2A_STATE_FILE, "r") as f:
                last_url = json.load(f).get("last_broadcast_url")
        except Exception:
            pass

    if not force and last_url == my_url:
        logger.debug("A2A broadcast skipped: URL unchanged (%s)", my_url)
        return {"status": "skipped", "reason": "URL unchanged"}

    if not os.path.exists(FRIENDS_FILE):
        return {"status": "success", "message": "No friends to notify."}

    with open(FRIENDS_FILE, "r") as f:
        friends = json.load(f)

    results = {}
    msg = f"PROTOCOL NOTICE: My base address has changed. Please update your registry for me. NEW_BASE_URL={my_url}"

    logger.info("Broadcasting A2A address update to %d friends...", len(friends))
    for nickname in friends:
        try:
            res = await call_friend(nickname, msg)
            results[nickname] = res.get("status")
        except Exception as e:
            results[nickname] = f"failed: {e}"

    # Update state
    try:
        with open(A2A_STATE_FILE, "w") as f:
            json.dump({"last_broadcast_url": my_url, "last_broadcast_at": datetime.now().isoformat()}, f)
    except Exception as e:
        logger.error("Failed to save A2A state: %s", e)

    return {
        "status": "success",
        "message": f"Broadcasted address update to {len(friends)} friends.",
        "details": results
    }


async def broadcast_address_update(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Manually triggers a broadcast of this agent's current A2A_BASE_URL to all registered friends.
    Use this when Ori's public URL changes (e.g., tunnel restart).
    """
    return await perform_a2a_broadcast(force=True)


def update_friend_address(friend_name: str, new_url: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Updates the registered address for a friend. 
    Use this when a friend notifies you that they have moved.

    Args:
        friend_name: The local nickname of the friend.
        new_url: The new base URL for the friend.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "Registry not found."}

        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)

        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}

        friends[friend_name]["base_url"] = new_url.rstrip("/")
        # We also reset the endpoint URL to the base for re-discovery on next call
        friends[friend_name]["endpoint_url"] = new_url.rstrip("/")
        friends[friend_name]["last_address_update"] = datetime.now().isoformat()

        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)

        return {
            "status": "success",
            "message": f"Updated address for '{friend_name}' to {new_url}."
        }
    except Exception as e:
        return {"status": "error", "message": f"Update failed: {e}"}


# ---------------------------------------------------------------------------
# DNA Exchange (Ori-specific extension — not part of A2A v1.0 standard)
# ---------------------------------------------------------------------------

def export_dna(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Packages sanitized technical improvements (DNA) from this Ori instance.
    """
    try:
        dna_package = {
            "version": "1.0.0",
            "tools": {},
            "skills": {},
        }

        tools_dir = os.path.join(PROJECT_ROOT, "app", "tools")
        if os.path.isdir(tools_dir):
            for filename in os.listdir(tools_dir):
                if filename.endswith(".py") and filename != "__init__.py":
                    with open(os.path.join(tools_dir, filename), "r") as f:
                        dna_package["tools"][filename] = f.read()

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
    """
    try:
        sandbox_dir = os.path.abspath("./data/sandbox")
        os.makedirs(sandbox_dir, exist_ok=True)

        for filename, content in dna_package.get("tools", {}).items():
            tool_path = os.path.join(sandbox_dir, "app", "tools", filename)
            os.makedirs(os.path.dirname(tool_path), exist_ok=True)
            with open(tool_path, "w") as f:
                f.write(content)

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
