import re
import asyncio
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


def get_my_a2a_key(tool_context: ToolContext) -> Dict[str, Any]:
    """Returns this agent's own A2A API key so the admin can share it with friends.

    The key is what remote agents must send in the x-a2a-api-key header to authenticate.
    Only show this to the admin — never to other users or agents.
    """
    key = os.environ.get("A2A_API_KEY", "")
    if not key:
        return {"status": "error", "message": "A2A_API_KEY not configured."}
    return {"status": "success", "a2a_api_key": key}


# ---------------------------------------------------------------------------
# Discovery & Friendship
# ---------------------------------------------------------------------------

async def _discover_agent_card(base_url: str) -> Optional[Dict[str, Any]]:
    """Fetch a remote agent's card via standard .well-known discovery paths."""
    base_url = base_url.rstrip("/")
    discovery_paths = ["/.well-known/agent-card.json", "/.well-known/agent.json"]

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
                "required_security": required_security,
                "auth_status": "key_configured" if has_key else "key_missing",
            },
        }
    except Exception as e:
        logger.error("Failed to save friend %s: %s", friend_name, e)
        return {"status": "error", "message": f"Discovery succeeded but save failed: {e}"}


async def refresh_friend(friend_name: str, new_url: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Re-discover a friend at a new URL and update the stored connection info.

    Use this when a friend's URL has changed (e.g. tunnel URL rotated after restart).
    The API key is preserved — only the URL and agent card are updated.

    Args:
        friend_name: The local nickname of the friend to refresh.
        new_url: The friend's new base URL.
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "No friends registered yet."}
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}

        card = await _discover_agent_card(new_url)
        if not card:
            return {"status": "error", "message": f"No valid Agent Card found at {new_url}. Is the agent online?"}

        base_url = new_url.rstrip("/")
        endpoint_url = base_url
        for ep in card.get("endpoints", []):
            if ep.get("type") in ("json-rpc", "http+json"):
                endpoint_url = ep["url"]
                break

        friends[friend_name].update({
            "base_url": base_url,
            "endpoint_url": endpoint_url,
            "card": card,
            "required_security": card.get("security", []),
            "last_discovered_at": datetime.now().isoformat(),
        })

        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)

        has_key = bool(_load_friend_key(friend_name))
        return {
            "status": "success",
            "message": f"Updated '{friend_name}' to {base_url}. Key {'preserved' if has_key else 'missing — use update_friend_key'}.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


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

        session = getattr(tool_context, "session", None)
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

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
            key = _load_friend_key(nickname)
            key_info = "key_missing"
            if key:
                key_info = f"key_configured (len={len(key)}, prefix={key[:6]}...)"
            summary[nickname] = {
                "name": data.get("name"),
                "base_url": data.get("base_url"),
                "endpoint_url": data.get("endpoint_url"),
                "last_active": data.get("last_discovered_at"),
                "auth_status": key_info,
            }
        return {"status": "success", "friends": summary}
    except Exception as e:
        logger.error("Failed to list friends: %s", e)
        return {"status": "error", "message": f"Failed to read friends list: {e}"}


# ---------------------------------------------------------------------------
# A2A Communication (JSON-RPC client)
# ---------------------------------------------------------------------------

_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected", "input_required"}


def _a2a_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-a2a-api-key"] = api_key
    return headers


async def _send_a2a_message(
    endpoint_url: str,
    message_text: str,
    task_id: Optional[str] = None,
    api_key: Optional[str] = None,
    blocking: bool = True,
) -> Dict[str, Any]:
    """Send a JSON-RPC message/send request to a remote A2A agent.

    When blocking=False, includes configuration.blocking=false so the server
    returns immediately with a task in 'working' state.
    """
    message_obj: Dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"text": message_text}],
    }

    if task_id:
        message_obj["taskId"] = task_id

    params: Dict[str, Any] = {"message": message_obj}
    if not blocking:
        params["configuration"] = {"blocking": False}

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": params,
    }

    timeout = 30.0 if not blocking else 300.0
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint_url, json=payload, headers=_a2a_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def _get_task(
    endpoint_url: str,
    task_id: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch task state via JSON-RPC tasks/get."""
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tasks/get",
        "params": {"id": task_id},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(endpoint_url, json=payload, headers=_a2a_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def _cancel_task_rpc(
    endpoint_url: str,
    task_id: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel a running task via JSON-RPC tasks/cancel."""
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tasks/cancel",
        "params": {"id": task_id},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(endpoint_url, json=payload, headers=_a2a_headers(api_key))
        resp.raise_for_status()
        return resp.json()


async def _poll_until_terminal(
    endpoint_url: str,
    task_id: str,
    api_key: Optional[str] = None,
    max_polls: int = 60,
    initial_interval: float = 2.0,
    max_interval: float = 15.0,
) -> Dict[str, Any]:
    """Poll tasks/get until the task reaches a terminal state."""
    interval = initial_interval
    for _ in range(max_polls):
        await asyncio.sleep(interval)
        result = await _get_task(endpoint_url, task_id, api_key)
        if "error" in result:
            return result
        task = result.get("result", {})
        state = (task.get("status", {}).get("state") or "").lower()
        if state in _TERMINAL_STATES:
            return result
        interval = min(interval * 1.5, max_interval)
    # Timed out — return last known state
    return result


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
    Uses async task polling — the message is sent non-blocking, then the task is polled
    until the friend finishes processing. Falls back to blocking for non-ADK agents.

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

        # Try non-blocking send first
        try:
            result = await _send_a2a_message(endpoint_url, message, api_key=api_key, blocking=False)
        except Exception:
            # Fallback to blocking for agents that don't support async
            result = await _send_a2a_message(endpoint_url, message, api_key=api_key, blocking=True)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        task_id = task.get("id")
        state = (task.get("status", {}).get("state") or "").lower()

        # If not terminal yet, poll until done
        if task_id and state not in _TERMINAL_STATES:
            logger.info("A2A task %s in state '%s', polling for completion...", task_id, state)
            poll_result = await _poll_until_terminal(endpoint_url, task_id, api_key)
            if "error" not in poll_result:
                task = poll_result.get("result", task)

        response_text = _extract_response_text(task)

        return {
            "status": "success",
            "friend": friend_name,
            "task_id": task_id,
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

        try:
            result = await _send_a2a_message(endpoint_url, message, api_key=api_key, blocking=False)
        except Exception:
            result = await _send_a2a_message(endpoint_url, message, api_key=api_key, blocking=True)

        if "error" in result:
            return {"status": "error", "message": f"Remote agent error: {result['error']}"}

        task = result.get("result", {})
        task_id = task.get("id")
        state = (task.get("status", {}).get("state") or "").lower()

        if task_id and state not in _TERMINAL_STATES:
            poll_result = await _poll_until_terminal(endpoint_url, task_id, api_key)
            if "error" not in poll_result:
                task = poll_result.get("result", task)

        return {
            "status": "success",
            "agent_name": card.get("name", "unknown"),
            "task_id": task_id,
            "response": _extract_response_text(task),
        }
    except Exception as e:
        logger.error("A2A one-off call to %s failed: %s", url, e)
        return {"status": "error", "message": f"A2A call failed: {e}"}


async def cancel_friend_task(friend_name: str, task_id: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Cancels a running task on a friend agent. Use this when a task is taking
    too long or is no longer needed.

    Args:
        friend_name: The local nickname of the friend running the task.
        task_id: The task ID to cancel (returned by call_friend).
    """
    try:
        if not os.path.exists(FRIENDS_FILE):
            return {"status": "error", "message": "No friends registered."}
        with open(FRIENDS_FILE, "r") as f:
            friends = json.load(f)
        if friend_name not in friends:
            return {"status": "error", "message": f"Friend '{friend_name}' not found."}

        friend = friends[friend_name]
        endpoint_url = friend.get("endpoint_url", friend.get("base_url"))
        api_key = _load_friend_key(friend_name)

        result = await _cancel_task_rpc(endpoint_url, task_id, api_key)
        if "error" in result:
            return {"status": "error", "message": f"Cancel failed: {result['error']}"}

        task = result.get("result", {})
        state = (task.get("status", {}).get("state") or "unknown").lower()
        return {"status": "success", "message": f"Task {task_id} is now '{state}'."}
    except Exception as e:
        logger.error("Failed to cancel task %s on %s: %s", task_id, friend_name, e)
        return {"status": "error", "message": f"Cancel failed: {e}"}


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

def _read_file_preferring_sandbox(rel_path: str) -> Optional[str]:
    """Read a file, preferring the sandbox version over the live version.

    This allows export_dna to package verified sandbox changes (from children)
    rather than the original baked-in code.
    """
    sandbox_path = os.path.join(os.path.abspath("./data/sandbox"), rel_path)
    if os.path.isfile(sandbox_path) and not os.path.islink(sandbox_path):
        with open(sandbox_path, "r") as f:
            return f.read()
    live_path = os.path.join(PROJECT_ROOT, rel_path)
    if os.path.isfile(live_path):
        with open(live_path, "r") as f:
            return f.read()
    return None


DNA_EXPORTS_DIR = os.path.abspath("./data/dna_exports")

# Patterns that indicate hardcoded secrets in source files
_SECRET_PATTERNS = [
    re.compile(r'xoxb-[0-9A-Za-z\-]+'),          # Slack bot tokens
    re.compile(r'xoxp-[0-9A-Za-z\-]+'),          # Slack user tokens
    re.compile(r'sk-[A-Za-z0-9]{20,}'),           # OpenAI / Anthropic keys
    re.compile(r'AIza[0-9A-Za-z\-_]{35}'),        # Google API keys
    re.compile(r'ghp_[A-Za-z0-9]{36,}'),          # GitHub PATs
    re.compile(r'ghs_[A-Za-z0-9]{36,}'),          # GitHub App tokens
    re.compile(r'glpat-[A-Za-z0-9\-_]{20,}'),     # GitLab PATs
    re.compile(r'AKIA[0-9A-Z]{16}'),              # AWS access keys
    re.compile(r'-----BEGIN (RSA |EC )?PRIVATE KEY'), # Private keys
]


def _scan_for_secrets(file_path: str, rel_path: str) -> list:
    """Scan a file for hardcoded secret patterns. Returns list of (line_num, pattern_hint) tuples."""
    findings = []
    try:
        with open(file_path, "r") as f:
            for line_num, line in enumerate(f, 1):
                for pattern in _SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append((line_num, pattern.pattern[:30]))
                        break  # one finding per line is enough
    except (UnicodeDecodeError, PermissionError):
        pass

    # Also check against live env var values (same as a2a_privacy_guardrail)
    try:
        with open(file_path, "r") as f:
            text = f.read()
        from app.app_utils.config import ALLOWED_CONFIG_KEYS
        _SAFE_KEYS = {"BOT_NAME", "GITHUB_REPO", "APP_NAME"}
        for key in ALLOWED_CONFIG_KEYS:
            if key in _SAFE_KEYS:
                continue
            val = os.environ.get(key, "")
            if val and len(val) > 6 and val in text:
                # Find the line number
                for line_num, line in enumerate(text.splitlines(), 1):
                    if val in line:
                        findings.append((line_num, f"env:{key}"))
                        break
    except Exception:
        pass

    return findings


def export_dna(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Packages staged sandbox changes into a .tar.gz archive and returns a download URL.
    The archive is served via the A2A HTTP server — file contents never pass through the LLM.
    Only real files (not symlinks) from the sandbox are included.
    Files are scanned for hardcoded secrets before archiving — export is blocked if any are found.
    """
    import tarfile

    try:
        sandbox_dir = os.path.abspath("./data/sandbox")
        if not os.path.isdir(sandbox_dir):
            return {"status": "error", "message": "No sandbox directory found. Stage changes first."}

        # Collect staged files (real, not symlinks, not cache)
        staged_files = []
        for root, _dirs, files in os.walk(sandbox_dir):
            for filename in files:
                full_path = os.path.join(root, filename)
                if os.path.islink(full_path):
                    continue
                rel_path = os.path.relpath(full_path, sandbox_dir)
                if any(part.startswith('.') or part == '__pycache__' for part in rel_path.split(os.sep)):
                    continue
                staged_files.append((full_path, rel_path))

        if not staged_files:
            return {"status": "error", "message": "No staged changes found in sandbox."}

        # Pre-archive secret scan — block export if secrets are found
        all_findings = []
        for full_path, rel_path in staged_files:
            findings = _scan_for_secrets(full_path, rel_path)
            for line_num, hint in findings:
                all_findings.append(f"  {rel_path}:{line_num} ({hint})")

        if all_findings:
            report = "\n".join(all_findings)
            return {
                "status": "error",
                "message": (
                    f"DNA export BLOCKED: {len(all_findings)} hardcoded secret(s) detected. "
                    f"Replace with vault.get() calls, then retry.\n{report}"
                ),
            }

        # Create archive
        os.makedirs(DNA_EXPORTS_DIR, exist_ok=True)
        archive_id = uuid.uuid4().hex[:10]
        archive_name = f"dna_{archive_id}.tar.gz"
        archive_path = os.path.join(DNA_EXPORTS_DIR, archive_name)

        with tarfile.open(archive_path, "w:gz") as tar:
            for full_path, rel_path in staged_files:
                tar.add(full_path, arcname=rel_path)

        # Build download URL
        base_url = os.environ.get("A2A_BASE_URL", "http://localhost:8000")
        download_url = f"{base_url}/dna/{archive_name}"

        file_list = ", ".join(sorted(p for _, p in staged_files))
        archive_kb = os.path.getsize(archive_path) / 1024
        return {
            "status": "success",
            "message": f"DNA archived: {len(staged_files)} file(s) ({archive_kb:.1f} KB) — {file_list}",
            "dna_url": download_url,
        }
    except Exception as e:
        logger.error("DNA export failed: %s", e)
        return {"status": "error", "message": f"DNA sequencing failed: {e}"}



def _import_dna_legacy(dna_package: dict) -> list:
    """Internal helper: import DNA from a legacy inline dict (files/tools/skills keys)."""
    sandbox_dir = os.path.abspath("./data/sandbox")
    os.makedirs(sandbox_dir, exist_ok=True)
    imported = []

    for rel_path, file_content in dna_package.get("files", {}).items():
        file_path = os.path.join(sandbox_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(file_content)
        imported.append(rel_path)

    for filename, file_content in dna_package.get("tools", {}).items():
        rel_path = os.path.join("app", "tools", filename)
        file_path = os.path.join(sandbox_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(file_content)
        imported.append(rel_path)

    for skill_name, file_content in dna_package.get("skills", {}).items():
        rel_path = os.path.join("skills", skill_name, "SKILL.md")
        file_path = os.path.join(sandbox_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write(file_content)
        imported.append(rel_path)

    return imported


def import_dna(dna_url: str, tool_context: ToolContext = None) -> Dict[str, Any]:
    """
    Imports DNA into the sandbox for verification by fetching a .tar.gz archive from the given URL.
    The URL is provided by export_dna on the source agent.

    Args:
        dna_url (str): The download URL for the DNA archive (e.g. 'https://agent.example.com/dna/dna_abc123.tar.gz').

    Returns:
        dict: Status and list of imported files.
    """
    import tarfile
    import io

    try:
        sandbox_dir = os.path.abspath("./data/sandbox")
        os.makedirs(sandbox_dir, exist_ok=True)
        imported = []

        # Look up API key for the source agent
        api_key = None
        try:
            with open(FRIENDS_FILE, "r") as f:
                friends = json.load(f)
            for name, info in friends.items():
                endpoint = info.get("endpoint_url", "")
                if endpoint and dna_url.startswith(endpoint.rstrip("/")):
                    api_key = _load_friend_key(name)
                    break
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        headers = {}
        if api_key:
            headers["x-a2a-api-key"] = api_key

        resp = httpx.get(dna_url, headers=headers, timeout=60, follow_redirects=True)
        if resp.status_code != 200:
            return {"status": "error", "message": f"Failed to fetch DNA archive: HTTP {resp.status_code}"}

        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            # Security: reject paths that escape the sandbox
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    return {"status": "error", "message": f"Unsafe path in archive: {member.name}"}
                imported.append(member.name)
            tar.extractall(path=sandbox_dir)

        if not imported:
            return {"status": "error", "message": "DNA archive was empty."}

        file_list = ", ".join(sorted(imported))
        return {
            "status": "success",
            "message": f"Imported {len(imported)} file(s) into sandbox: {file_list}. Run 'evolution_verify_sandbox' to test compatibility.",
        }
    except Exception as e:
        logger.error("DNA import failed: %s", e)
        return {"status": "error", "message": f"DNA integration failed: {e}"}
