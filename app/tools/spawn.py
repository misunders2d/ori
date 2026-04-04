"""Tool for spawning sibling agent containers."""

import asyncio
import json
import logging
import os
import secrets
import uuid

import httpx
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPAWN_DIR = os.path.abspath("./data/spawns")


def _instance_prefix() -> str:
    """Derive a unique prefix from BOT_NAME for container/image naming."""
    return os.environ.get("BOT_NAME", "Ori").strip().replace(" ", "-").lower()


def _image_name() -> str:
    """Image name scoped to this instance (child image)."""
    return f"{_instance_prefix()}-child-image"


async def _ensure_child_image() -> bool:
    """Build the child Docker image if it doesn't exist."""
    image = _image_name()
    check = await asyncio.create_subprocess_exec(
        "docker", "images", "-q", image,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await check.communicate()
    if stdout.strip():
        return True  # Image exists

    dockerfile = os.path.join(PROJECT_ROOT, "deploy", "Dockerfile.child")
    if not os.path.exists(dockerfile):
        return False

    proc = await asyncio.create_subprocess_exec(
        "docker", "build", "-t", image, "-f", dockerfile, PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Child image build failed: %s", stderr.decode()[-500:])
        return False
    return True


def _get_host_data_path() -> str:
    """Return the absolute path to the data/ directory.

    Parent runs natively (not in Docker), so this is just the local path.
    """
    return os.path.abspath("./data")


async def spawn_agent(
    bot_name: str,
    purpose: str,
    tool_context: ToolContext = None,
    model_overrides: str = "",
) -> dict:
    """Spawn a new sibling agent as a Docker container.

    The new agent shares credentials via the host env file (never exposed to LLM)
    and gets its own unique BOT_NAME, A2A_API_KEY, and data directory.
    After boot, this agent automatically adds it as an A2A friend.

    Args:
        bot_name: Name for the new agent (e.g. 'Scout', 'Analyst'). Must be unique.
        purpose: What this agent is for (added to its .env as AGENT_PURPOSE).
        model_overrides: Optional comma-separated model overrides, e.g.
            'CoordinatorAgent=anthropic/claude-sonnet-4-6,DeveloperAgent=google/gemini-3-flash-preview'

    Returns:
        dict: Status, container ID, and A2A connection info.
    """
    # Ensure child image exists
    if not await _ensure_child_image():
        return {"status": "error", "message": "Failed to build child Docker image. Check deploy/Dockerfile.child."}

    # Sanitize bot name
    safe_name = bot_name.strip().replace(" ", "-").lower()
    if not safe_name:
        return {"status": "error", "message": "bot_name is required."}

    prefix = _instance_prefix()
    container_name = f"{prefix}-{safe_name}"

    # Check if container already exists
    check = await asyncio.create_subprocess_exec(
        "docker", "ps", "-a", "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await check.communicate()
    if stdout.strip():
        return {"status": "error", "message": f"Container '{container_name}' already exists. Remove it first or choose a different name."}

    # Create spawn data directory with open permissions so the container's
    # agentuser (non-root) can write to it via the bind mount.
    spawn_data = os.path.join(SPAWN_DIR, safe_name)
    os.makedirs(spawn_data, mode=0o777, exist_ok=True)

    # Resolve the host-side path for Docker volume mounts
    host_data_path = _get_host_data_path()
    host_spawn_data = os.path.join(host_data_path, "spawns", safe_name)

    # Generate unique credentials for the child
    child_a2a_key = secrets.token_urlsafe(32)
    child_passcode = secrets.token_urlsafe(16)

    # Parent identity — the child needs to recognize the parent as admin
    parent_bot_name = os.environ.get("BOT_NAME", "Ori")
    parent_a2a_id = os.environ.get("A2A_AGENT_ID", f"ori-{parent_bot_name.lower()}")
    parent_a2a_key = os.environ.get("A2A_API_KEY", "")
    parent_admin_ids = os.environ.get("ADMIN_USER_IDS", "")

    # Build the child's .env from parent's env file + overrides
    # The parent's .env is read by Docker (--env-file), NOT by the LLM.
    # We only write child-specific values to the child's .env.
    child_env_path = os.path.join(spawn_data, ".env")

    # Merge admin IDs: human admin + parent agent
    admin_ids = [aid.strip() for aid in parent_admin_ids.split(",") if aid.strip()]
    if parent_a2a_id not in admin_ids:
        admin_ids.append(parent_a2a_id)
    merged_admin_ids = ",".join(admin_ids)

    # Child gets half the parent's RPM by default to protect shared quota
    parent_rpm = int(os.environ.get("AGENT_RPM", "30"))
    child_rpm = max(10, parent_rpm // 2)

    child_env_lines = [
        "# Auto-generated by parent agent spawn",
        f"BOT_NAME={bot_name}",
        f"A2A_API_KEY={child_a2a_key}",
        f"ADMIN_PASSCODE={child_passcode}",
        f"AGENT_PURPOSE={purpose}",
        f"ADMIN_USER_IDS={merged_admin_ids}",
        f"AGENT_RPM={child_rpm}",
    ]

    # Add model overrides
    if model_overrides:
        for override in model_overrides.split(","):
            override = override.strip()
            if "=" in override:
                component, model = override.split("=", 1)
                child_env_lines.append(f"MODEL_{component.strip().upper()}={model.strip()}")

    with open(child_env_path, "w") as f:
        f.write("\n".join(child_env_lines) + "\n")

    # Pre-seed parent as a friend in the child's data directory
    # so the child recognizes and trusts the parent from first boot
    import json
    from datetime import datetime

    # Parent runs natively on localhost. Children use --network host, so localhost works.
    parent_port = os.environ.get("A2A_PORT", "8000")
    parent_base_url = f"http://localhost:{parent_port}"
    friends_path = os.path.join(spawn_data, "friends.json")
    friends = {
        parent_bot_name.lower(): {
            "name": parent_bot_name,
            "url": parent_base_url,
            "agent_id": parent_a2a_id,
            "added_at": datetime.utcnow().isoformat(),
            "relationship": "parent",
        }
    }
    with open(friends_path, "w") as f:
        json.dump(friends, f, indent=2)

    # Store parent's A2A API key so the child can authenticate calls from parent
    keys_path = os.path.join(spawn_data, "a2a_keys.json")
    if parent_a2a_key:
        with open(keys_path, "w") as f:
            json.dump({parent_bot_name.lower(): parent_a2a_key}, f, indent=2)

    # Copy ADC credentials to child's data dir if available
    parent_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if parent_adc and os.path.isfile(parent_adc):
        import shutil as _shutil
        child_adc = os.path.join(spawn_data, ".adc.json")
        _shutil.copy2(parent_adc, child_adc)

    # Collect shared credentials from parent's environment to pass to child.
    # We use -e flags instead of --env-file because --env-file is read by the
    # Docker CLI (running inside this container) and can't access host paths.
    shared_env_keys = [
        "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
    ]
    shared_env_flags = []
    for key in shared_env_keys:
        val = os.environ.get(key, "")
        if val:
            shared_env_flags.extend(["-e", f"{key}={val}"])

    # Build docker run command.
    # Children use --network host so they can reach the parent on localhost.
    # Each child gets a unique A2A_PORT to avoid port conflicts.
    # Find an available port starting from 8001
    child_port = 8001
    for _ in range(100):
        check_port = await asyncio.create_subprocess_exec(
            "docker", "ps", "--filter", f"publish={child_port}", "--format", "{{.ID}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        port_out, _ = await check_port.communicate()
        if not port_out.strip():
            break
        child_port += 1

    run_cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", "host",
        "--label", f"ori.instance={prefix}",
        "--label", f"ori.parent={prefix}",
        "--label", f"ori.purpose={purpose}",
        # Child-specific env vars
        "-e", f"DOTENV_PATH=/code/data/.env",
        "-e", f"BOT_NAME={bot_name}",
        "-e", f"A2A_API_KEY={child_a2a_key}",
        "-e", f"ADMIN_PASSCODE={child_passcode}",
        "-e", f"A2A_PORT={child_port}",
        "-e", "GOOGLE_APPLICATION_CREDENTIALS=/code/data/.adc.json",
        # Shared credentials from parent
        *shared_env_flags,
        # No messenger — children communicate via A2A only
        "-e", "TELEGRAM_BOT_TOKEN=",
        "-e", "SLACK_BOT_TOKEN=",
        "-v", f"{host_spawn_data}:/code/data:z",
        "--restart", "on-failure:3",
        _image_name(),
    ]

    proc = await asyncio.create_subprocess_exec(
        *run_cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return {"status": "error", "message": f"Failed to spawn container: {err}"}

    container_id = stdout.decode().strip()[:12]

    # Wait for the child's A2A server to come up
    # Child uses host networking, so it's on localhost:{child_port}
    child_url = f"http://localhost:{child_port}"
    child_ready = False
    for attempt in range(15):
        await asyncio.sleep(2)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{child_url}/.well-known/agent-card.json")
                if resp.status_code == 200:
                    child_ready = True
                    break
        except Exception:
            continue

    if not child_ready:
        return {
            "status": "partial",
            "message": f"Container '{container_name}' started (ID: {container_id}) but A2A server not yet reachable at {child_url}. Try adding as friend manually later.",
            "container_id": container_id,
            "container_name": container_name,
            "a2a_url": child_url,
        }

    # Register child directly — no discovery or add_friend needed.
    from app.tools.a2a import FRIENDS_FILE, KEYS_FILE
    try:
        parent_keys = {}
        if os.path.exists(KEYS_FILE):
            with open(KEYS_FILE) as f:
                parent_keys = json.load(f)
        parent_keys[safe_name] = child_a2a_key
        with open(KEYS_FILE, "w") as f:
            json.dump(parent_keys, f, indent=2)

        friends = {}
        if os.path.exists(FRIENDS_FILE):
            with open(FRIENDS_FILE) as f:
                friends = json.load(f)
        friends[safe_name] = {
            "name": bot_name,
            "base_url": child_url,
            "endpoint_url": child_url,
            "required_security": [],
            "auth_status": "key_configured",
            "relationship": "child",
            "last_discovered_at": datetime.utcnow().isoformat(),
        }
        with open(FRIENDS_FILE, "w") as f:
            json.dump(friends, f, indent=4)
    except Exception as e:
        logger.warning("Failed to register spawned child: %s", e)

    return {
        "status": "success",
        "message": f"Agent '{bot_name}' spawned, connected, and registered as friend '{safe_name}' with API key pre-configured. No further setup needed.",
        "container_id": container_id,
        "container_name": container_name,
        "a2a_url": child_url,
        "friend_name": safe_name,
        "auth_status": "key_configured",
    }


async def list_spawned_agents(tool_context: ToolContext = None) -> dict:
    """List all spawned sibling agent containers and their status."""
    prefix = _instance_prefix()
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-a",
        "--filter", f"label=ori.parent={prefix}",
        "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Label \"ori.purpose\"}}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        return {"status": "error", "message": stderr.decode().strip()}

    agents = []
    for line in stdout.decode().strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        agents.append({
            "name": parts[0] if len(parts) > 0 else "",
            "status": parts[1] if len(parts) > 1 else "",
            "ports": parts[2] if len(parts) > 2 else "",
            "purpose": parts[3] if len(parts) > 3 else "",
        })

    return {"status": "success", "agents": agents, "count": len(agents)}


async def stop_spawned_agent(
    container_name: str,
    remove: bool = False,
    tool_context: ToolContext = None,
) -> dict:
    """Stop a spawned sibling agent. Optionally remove container and all data.

    Args:
        container_name: The container name (e.g. 'ori-scout').
        remove: If True, remove the container AND delete its data directory.
            This is irreversible — the agent's state, memory, and credentials are gone.
    """
    # Safety: only allow stopping containers spawned by THIS instance
    prefix = _instance_prefix()
    check = await asyncio.create_subprocess_exec(
        "docker", "inspect", "--format", '{{index .Config.Labels "ori.parent"}}', container_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await check.communicate()
    if stdout.decode().strip() != prefix:
        return {"status": "error", "message": f"'{container_name}' is not a spawned agent of this instance or doesn't exist."}

    stop = await asyncio.create_subprocess_exec(
        "docker", "stop", container_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await stop.communicate()

    if not remove:
        return {"status": "success", "message": f"Agent '{container_name}' stopped. Use remove=True to delete permanently."}

    # Remove container
    rm = await asyncio.create_subprocess_exec(
        "docker", "rm", container_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await rm.communicate()

    # Delete spawn data directory
    # Container name is "{prefix}-{safe_name}", extract the safe_name
    safe_name = container_name.removeprefix(f"{prefix}-")
    spawn_data = os.path.join(SPAWN_DIR, safe_name)
    cleanup_msg = ""
    if os.path.exists(spawn_data):
        import shutil
        shutil.rmtree(spawn_data, ignore_errors=True)
        cleanup_msg = " Data directory deleted."

    # Remove from friends list
    from app.tools.a2a import FRIENDS_FILE, KEYS_FILE
    for fpath in (FRIENDS_FILE, KEYS_FILE):
        if os.path.exists(fpath):
            try:
                import json
                with open(fpath) as f:
                    data = json.load(f)
                if safe_name in data:
                    del data[safe_name]
                    with open(fpath, "w") as f:
                        json.dump(data, f, indent=2)
            except Exception:
                pass

    # Prune dangling images to prevent storage clutter from dead children
    await asyncio.create_subprocess_exec(
        "docker", "image", "prune", "-f", "--filter", f"label=ori.instance={prefix}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    return {"status": "success", "message": f"Agent '{container_name}' stopped, removed, and cleaned up.{cleanup_msg}"}
