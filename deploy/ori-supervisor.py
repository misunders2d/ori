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


def sync_deps():
    """Run uv sync to install dependencies."""
    logger.info("Syncing dependencies (uv sync)...")
    result = subprocess.run(
        ["uv", "sync"], cwd=PROJECT_ROOT,
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
        push_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"
        try:
            subprocess.run(
                ["git", "fetch", "origin", "master"],
                cwd=PROJECT_ROOT, capture_output=True, timeout=60,
            )
            subprocess.run(
                ["git", "reset", "--hard", "origin/master"],
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


def run_setup_wizard():
    """Run interactive setup wizard if no LLM provider is configured."""
    vault_data = get_all()
    has_google = bool(vault_data.get("GOOGLE_API_KEY", ""))
    has_vertex = vault_data.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    has_anthropic = bool(vault_data.get("ANTHROPIC_API_KEY", ""))

    if has_google or has_vertex or has_anthropic:
        return  # Already configured

    if not sys.stdin.isatty():
        logger.error("No LLM provider configured and no interactive terminal. Run setup wizard manually.")
        sys.exit(1)

    logger.info("No LLM provider configured. Launching setup wizard...")
    wizard = os.path.join(PROJECT_ROOT, "interfaces", "setup_wizard.py")
    subprocess.run([sys.executable, wizard], cwd=PROJECT_ROOT)
    # Reload vault after wizard writes credentials
    load_vault()


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

    bot_name = get("BOT_NAME") or "Ori"
    logger.info("Bot: %s", bot_name)

    run_setup_wizard()

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
            crash_count = 0
            continue

        elif exit_code == 101:
            # Rollback: revert one commit, sync deps, restart
            logger.info("Rollback signal (101). Reverting...")
            apply_rollback()
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
