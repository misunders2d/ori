#!/usr/bin/env python3
"""Ori Process Supervisor — manages the agent as a native Python subprocess.

Replaces launcher.sh. Responsibilities:
  - Load vault credentials into environment
  - Generate secrets if missing (ADMIN_PASSCODE, A2A_API_KEY)
  - Run run_bot.py as a subprocess
  - Handle exit signals: 100 (evolution), 101 (rollback), 0 (clean shutdown)
  - No automatic rollback on crash — stop and alert instead
  - Run `uv sync` if pyproject.toml changed during evolution

Usage:
  python deploy/ori-supervisor.py        # foreground
  systemd ExecStart=.venv/bin/python deploy/ori-supervisor.py
"""

import logging
import logging.handlers
import os
import secrets
import shutil
import subprocess
import sys
import time

# Resolve project root (parent of deploy/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(PROJECT_ROOT)

# Add project root to Python path so imports work
sys.path.insert(0, PROJECT_ROOT)

from deploy.vault import (
    VAULT_FILE,
    load_vault,
    get,
    set as vault_set,
    get_all,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(PROJECT_ROOT, "data", "agent.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=200_000, backupCount=2),
    ],
)

logger = logging.getLogger("ori-supervisor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SIGNAL_FILE = os.path.join(PROJECT_ROOT, "data", ".exit_signal")
MAX_CRASHES = 3
COOLDOWN = 30
STABLE_THRESHOLD = 60
PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ensure_secrets():
    """Generate ADMIN_PASSCODE and A2A_API_KEY if not in vault."""
    vault_data = get_all()
    changes = {}
    if not vault_data.get("ADMIN_PASSCODE"):
        changes["ADMIN_PASSCODE"] = secrets.token_urlsafe(16)
        logger.info("Generated ADMIN_PASSCODE")
    if not vault_data.get("A2A_API_KEY"):
        changes["A2A_API_KEY"] = "ori-" + secrets.token_urlsafe(24)
        logger.info("Generated A2A_API_KEY")
    if changes:
        from deploy.vault import set_many
        set_many(changes)


def read_exit_signal() -> int | None:
    """Read and consume the exit signal file. Returns signal code or None."""
    if not os.path.exists(SIGNAL_FILE):
        return None
    try:
        with open(SIGNAL_FILE) as f:
            code = int(f.read().strip())
        os.unlink(SIGNAL_FILE)
        return code
    except (ValueError, OSError):
        try:
            os.unlink(SIGNAL_FILE)
        except OSError:
            pass
        return None


def deps_changed() -> bool:
    """Check if pyproject.toml or uv.lock changed since last sync."""
    fingerprint_file = os.path.join(PROJECT_ROOT, "data", ".deps_hash")
    import hashlib
    hasher = hashlib.sha256()
    for fname in ["pyproject.toml", "uv.lock"]:
        path = os.path.join(PROJECT_ROOT, fname)
        if os.path.exists(path):
            with open(path, "rb") as f:
                hasher.update(f.read())
    current_hash = hasher.hexdigest()

    if os.path.exists(fingerprint_file):
        with open(fingerprint_file) as f:
            if f.read().strip() == current_hash:
                return False

    with open(fingerprint_file, "w") as f:
        f.write(current_hash)
    return True


def _find_uv() -> str:
    """Find the uv binary, checking common install locations."""
    for candidate in [
        shutil.which("uv"),
        os.path.expanduser("~/.local/bin/uv"),
        os.path.expanduser("~/.cargo/bin/uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "uv"  # fallback to PATH


def sync_deps():
    """Run uv sync to install dependencies."""
    uv = _find_uv()
    logger.info("Syncing dependencies (%s sync)...", uv)
    result = subprocess.run(
        [uv, "sync"], cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        logger.error("uv sync failed: %s", result.stderr[-500:])
        return False
    logger.info("Dependencies synced")
    return True


def apply_evolution():
    """After exit 100: pull from remote if configured, update files, sync deps."""
    logger.info("Applying evolution...")

    # Pull from remote if configured
    github_token = get("GITHUB_TOKEN")
    github_repo = get("GITHUB_REPO")
    if github_token and github_repo:
        fetch_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"
        try:
            subprocess.run(
                ["git", "fetch", fetch_url, "master"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=60,
            )
            subprocess.run(
                ["git", "reset", "--hard", "FETCH_HEAD"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "clean", "-fd", "--exclude=data"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=30,
            )
            logger.info("Pulled latest from remote")
        except Exception as e:
            logger.error("Git pull failed: %s", e)
    else:
        # Local evolution: files are already committed to master by the worktree flow
        # Just checkout to update working tree from git state
        try:
            subprocess.run(
                ["git", "checkout", "master", "--", "."],
                cwd=PROJECT_ROOT, capture_output=True, timeout=30,
            )
        except Exception as e:
            logger.error("Git checkout failed: %s", e)

    # Sync dependencies if they changed
    if deps_changed():
        sync_deps()

    # Rebuild child image only if children are currently running
    try:
        result = subprocess.run(
            ["docker", "ps", "-q", "--filter", f"label=ori.parent={(get('BOT_NAME') or 'ori').strip().replace(' ', '-').lower()}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip():
            rebuild_child_image()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # No Docker or timed out — skip


def rebuild_child_image():
    """Rebuild the child Docker image if Docker is available."""
    dockerfile = os.path.join(PROJECT_ROOT, "deploy", "Dockerfile.child")
    if not os.path.exists(dockerfile):
        return
    try:
        # Derive image name from BOT_NAME
        bot_name = get("BOT_NAME") or "ori"
        image_name = f"{bot_name.strip().replace(' ', '-').lower()}-child-image"
        logger.info("Rebuilding child image: %s", image_name)
        result = subprocess.run(
            ["docker", "build", "-t", image_name, "-f", dockerfile, PROJECT_ROOT],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning("Child image rebuild failed (non-critical): %s", result.stderr[-300:])
        else:
            logger.info("Child image rebuilt successfully")
            # Prune old dangling images
            subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, timeout=30)
    except FileNotFoundError:
        pass  # Docker not installed — no children to rebuild
    except Exception as e:
        logger.warning("Child image rebuild skipped: %s", e)



def refresh_tunnel():
    """Restart the Cloudflare tunnel container with correct ports from vault."""
    if not shutil.which("docker"):
        return
    bot_name = (get("BOT_NAME") or "ori").strip().replace(" ", "-").lower()
    tunnel_name = "".join(c for c in bot_name if c.isalnum() or c == "-")
    a2a_port = get("A2A_PORT") or "8000"
    metrics_port = get("TUNNEL_METRICS_PORT") or str(int(a2a_port) + 1000)
    compose_file = os.path.join(PROJECT_ROOT, "deploy", "docker-compose.yml")
    if not os.path.exists(compose_file):
        return
    env = {
        **os.environ,
        "A2A_PORT": a2a_port,
        "BOT_NAME": tunnel_name,
        "TUNNEL_METRICS_PORT": metrics_port,
    }
    # Export so run_bot.py can detect the tunnel on the correct port
    os.environ["TUNNEL_METRICS_PORT"] = metrics_port
    try:
        # Kill any existing tunnel container for this bot (by name, regardless of how it was created)
        subprocess.run(
            ["docker", "rm", "-f", f"{tunnel_name}-tunnel"],
            capture_output=True, text=True, timeout=15,
        )
        # Start fresh tunnel scoped by bot name
        subprocess.run(
            ["docker", "compose", "-p", tunnel_name, "-f", compose_file, "up", "-d"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        logger.info("Tunnel refreshed (port %s, metrics %s)", a2a_port, metrics_port)
    except Exception as e:
        logger.warning("Tunnel refresh failed: %s", e)


def apply_rollback():
    """After exit 101: revert one commit, sync deps."""
    logger.info("Applying rollback (HEAD~1)...")
    try:
        subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=PROJECT_ROOT, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "clean", "-fd", "--exclude=data"],
            cwd=PROJECT_ROOT, capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.error("Rollback git operations failed: %s", e)

    if deps_changed():
        sync_deps()


def copy_adc():
    """Copy Google ADC credentials into data/ if available."""
    home = os.environ.get("HOME", os.path.expanduser("~"))
    adc_host = os.path.join(home, ".config", "gcloud", "application_default_credentials.json")
    adc_data = os.path.join(PROJECT_ROOT, "data", ".adc.json")
    if os.path.exists(adc_host):
        try:
            shutil.copy2(adc_host, adc_data)
            os.chmod(adc_data, 0o644)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_data
            logger.info("ADC credentials copied")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main supervisor loop
# ---------------------------------------------------------------------------

def main():
    bot_name = "Ori"
    logger.info("=== %s Supervisor starting ===", bot_name)

    # Initialize
    os.makedirs(os.path.join(PROJECT_ROOT, "data"), exist_ok=True)
    load_vault()
    ensure_secrets()
    copy_adc()

    # Auto-assign A2A_PORT if not configured — find a free port starting from 8000
    if not get("A2A_PORT"):
        import socket
        port = 8000
        for _ in range(100):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    break
            port += 1
        vault_set("A2A_PORT", str(port))
        os.environ["A2A_PORT"] = str(port)
        logger.info("Auto-assigned A2A_PORT=%d", port)

    # Start/refresh tunnel with the correct port before launching the bot
    refresh_tunnel()

    # Signal to run_bot.py that vault is already loaded (skip double-load)
    os.environ["_VAULT_LOADED"] = "1"

    bot_name = get("BOT_NAME") or "Ori"
    logger.info("Bot: %s", bot_name)

    # Verify LLM provider is configured (setup wizard runs from start.sh, not here)
    vault_data = get_all()
    has_provider = (
        bool(vault_data.get("GOOGLE_API_KEY"))
        or vault_data.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        or bool(vault_data.get("ANTHROPIC_API_KEY"))
    )
    if not has_provider:
        logger.error(
            "No LLM provider configured. Run 'deploy/start.sh' from an "
            "interactive terminal to complete the setup wizard."
        )
        sys.exit(1)

    # Initial dep sync if needed
    if deps_changed():
        if not sync_deps():
            logger.error("Initial dependency sync failed")
            sys.exit(1)

    crash_count = 0
    last_start = 0

    while True:
        # Clear any stale signal file
        if os.path.exists(SIGNAL_FILE):
            os.unlink(SIGNAL_FILE)

        logger.info("Starting %s...", bot_name)
        last_start = time.monotonic()

        proc = subprocess.Popen(
            [PYTHON, "run_bot.py"],
            cwd=PROJECT_ROOT,
        )

        try:
            proc.wait()
        except KeyboardInterrupt:
            logger.info("Interrupt received, stopping %s...", bot_name)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break

        exit_code = proc.returncode

        # Check for signal file (agent writes intent before clean exit)
        signal = read_exit_signal()
        if signal is not None:
            exit_code = signal

        uptime = time.monotonic() - last_start

        logger.info("Process exited with code %d (uptime: %.0fs, signal: %s)",
                     proc.returncode, uptime, signal)

        # Reset crash counter if the process was stable
        if uptime > STABLE_THRESHOLD:
            crash_count = 0

        if exit_code == 100:
            # Evolution: pull/update code, sync deps, restart
            logger.info("Evolution signal (100). Applying changes...")
            apply_evolution()
            refresh_tunnel()
            crash_count = 0
            continue

        elif exit_code == 101:
            # Rollback: revert one commit, sync deps, restart
            logger.info("Rollback signal (101). Reverting...")
            apply_rollback()
            refresh_tunnel()
            crash_count = 0
            continue

        elif exit_code == 0:
            # Clean shutdown
            logger.info("Clean shutdown. Goodbye.")
            break

        else:
            # Unexpected crash
            crash_count += 1
            logger.warning("Unexpected exit (code %d). Crash %d/%d.",
                          exit_code, crash_count, MAX_CRASHES)

            if crash_count >= MAX_CRASHES:
                logger.error(
                    "CRASH LIMIT REACHED (%d consecutive crashes). "
                    "Stopping supervisor. Manual intervention required. "
                    "Check data/agent.log for details.",
                    MAX_CRASHES,
                )
                sys.exit(1)

            logger.info("Cooling down %ds before restart...", COOLDOWN)
            time.sleep(COOLDOWN)


if __name__ == "__main__":
    main()
