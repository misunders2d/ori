#!/usr/bin/env bash
# ============================================================================
# Ori Launcher — Platform-aware recovery supervisor
# ============================================================================
# This script is the ONLY thing the user runs. It owns all recovery logic.
# It must NEVER be modified by Ori's self-evolution (DeveloperAgent).
#
# Recovery chain:
#   systemd/launchd (auto-start) -> launcher.sh (rollback) -> docker compose (the bot)
#
# Supports: Linux, macOS, Windows (WSL)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Configuration ---
IMAGE_NAME="ori-agent-image"
CRASH_FILE="data/.crash_count"
MAX_CRASHES=3
COOLDOWN=30
STABLE_THRESHOLD=60  # seconds before declaring a boot "stable"

# --- Helper: read BOT_NAME from .env ---
read_bot_name() {
    local name="Ori"
    if [ -f "data/.env" ]; then
        local env_name
        env_name=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d "\"'\\r" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' 2>/dev/null || true)
        [ -n "$env_name" ] && name="$env_name"
    fi
    echo "$name"
}

BOT_NAME=$(read_bot_name)
log() { echo ":: [$BOT_NAME Launcher] $*"; }

# --- Helper: read crash counter ---
read_crashes() {
    if [ -f "$CRASH_FILE" ]; then
        local val
        val=$(cat "$CRASH_FILE" 2>/dev/null || echo "0")
        [[ "$val" =~ ^[0-9]+$ ]] && echo "$val" || echo "0"
    else
        echo "0"
    fi
}

# --- Helper: detect if rebuild is needed ---
needs_rebuild() {
    # No image at all
    if [[ "$(docker images -q "$IMAGE_NAME" 2>/dev/null)" == "" ]]; then
        return 0
    fi
    # Dependency or Dockerfile changes since last build
    if [ -f "data/.last_build" ]; then
        [ "pyproject.toml" -nt "data/.last_build" ] && return 0
        [ "Dockerfile" -nt "data/.last_build" ] && return 0
        [ "uv.lock" -nt "data/.last_build" ] && return 0
    else
        return 0  # no record of last build
    fi
    return 1
}

# --- Helper: detect if deps changed (needs --no-cache) vs code-only change ---
deps_changed() {
    # Compare dep files against a separate fingerprint
    local fingerprint="data/.deps_hash"
    local current_hash
    current_hash=$(cat pyproject.toml uv.lock Dockerfile 2>/dev/null | sha256sum | cut -d' ' -f1)
    if [ -f "$fingerprint" ] && [ "$(cat "$fingerprint")" = "$current_hash" ]; then
        return 1  # deps unchanged
    fi
    echo "$current_hash" > "$fingerprint"
    return 0  # deps changed
}

# --- Helper: build image and clean up old images ---
smart_build() {
    if deps_changed; then
        log "Dependencies changed. Full rebuild (--no-cache)..."
        docker compose build --no-cache
    else
        log "Code-only change. Incremental rebuild..."
        docker compose build
    fi
    touch data/.last_build
    # Always prune dangling images after build
    docker image prune -f --filter "label=project=ori" 2>/dev/null || true
}

# --- Helper: detect interactive mode ---
is_interactive() {
    if [ -f "data/.env" ] && grep -qE "^(TELEGRAM_BOT_TOKEN|SLACK_BOT_TOKEN)=" data/.env 2>/dev/null; then
        return 1  # messenger configured, not interactive
    fi
    return 0  # no messenger, run interactive CLI
}

# --- Export host UID/GID for container permission alignment ---
export AGENT_UID="${AGENT_UID:-$(id -u)}"
export AGENT_GID="${AGENT_GID:-$(id -g)}"

# --- First-time setup wizard ---
# Require at least one LLM provider to be configured before starting.
needs_setup() {
    [ ! -f "data/.env" ] && return 0
    # Check for any provider: API key or Vertex AI mode
    grep -q "GOOGLE_API_KEY=" "data/.env" 2>/dev/null && return 1
    grep -q "ANTHROPIC_API_KEY=" "data/.env" 2>/dev/null && return 1
    grep -qi "GOOGLE_GENAI_USE_VERTEXAI=TRUE" "data/.env" 2>/dev/null && return 1
    return 0
}

if needs_setup; then
    log "No LLM provider configured. Launching setup wizard..."
    docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
fi

# ============================================================================
# THE REGENERATION LOOP
# ============================================================================
log "Starting regeneration loop..."

while true; do
    CRASHES=$(read_crashes)
    BOT_NAME=$(read_bot_name)  # re-read in case .env changed

    # --- CRASH LOOP RECOVERY ---
    if [ "$CRASHES" -ge "$MAX_CRASHES" ]; then
        log "ALERT: $CRASHES consecutive crashes detected. Initiating rollback..."

        # Preserve .env before git operations
        [ -f "data/.env" ] && cp -a data/.env data/.env.rollback 2>/dev/null || true

        git fetch origin master 2>/dev/null || true
        git reset --hard HEAD~1
        git clean -fd --exclude=data --exclude='data/*' --exclude=.env

        # Restore .env if git wiped it
        if [ ! -f "data/.env" ] && [ -f "data/.env.rollback" ]; then
            mv data/.env.rollback data/.env
            log "Restored .env from backup."
        fi

        echo "0" > "$CRASH_FILE"
        log "Rollback complete. Rebuilding..."
        smart_build
    fi

    # --- INCREMENT CRASH COUNTER (reset on stable boot) ---
    echo "$((CRASHES + 1))" > "$CRASH_FILE"

    # --- BUILD IF NEEDED ---
    BUILD_FLAG=""
    if needs_rebuild; then
        log "Build required (new image or dependency change)."
        BUILD_FLAG="--build"
    fi

    # --- LAUNCH ---
    if is_interactive; then
        log "No messenger configured. Launching interactive CLI..."
        docker compose up -d cloudflare-tunnel 2>/dev/null || true
        docker compose run --rm -it --service-ports ori-agent
        EXIT_CODE=$?
    else
        docker compose up $BUILD_FLAG &
        COMPOSE_PID=$!

        # Background stability check: if boot survives STABLE_THRESHOLD, reset crash counter
        (
            sleep "$STABLE_THRESHOLD"
            if kill -0 "$COMPOSE_PID" 2>/dev/null; then
                log "Boot stable for ${STABLE_THRESHOLD}s. Resetting crash counter."
                echo "0" > "$CRASH_FILE"
            fi
        ) &
        STABILITY_PID=$!

        wait "$COMPOSE_PID" || true
        EXIT_CODE=$?
        kill "$STABILITY_PID" 2>/dev/null || true
    fi

    touch data/.last_build

    # --- SIGNAL HANDLING ---
    case $EXIT_CODE in
        100)
            # Evolution signal: Ori committed changes, sync and rebuild
            log "Evolution signal (100). Syncing and rebuilding..."
            # Pull from remote if configured, otherwise changes are already local
            if git remote get-url origin &>/dev/null; then
                git fetch origin master 2>/dev/null || true
                git reset --hard origin/master
                git clean -fd --exclude=data --exclude='data/*' --exclude=.env
            fi
            smart_build
            echo "0" > "$CRASH_FILE"
            ;;
        101)
            # Rollback signal: Ori requested manual rollback
            log "Rollback signal (101). Reverting to previous commit..."
            git reset --hard HEAD~1
            git clean -fd --exclude=data --exclude='data/*' --exclude=.env
            smart_build
            echo "0" > "$CRASH_FILE"
            ;;
        0|130)
            # Clean exit or Ctrl+C
            log "Clean shutdown. Goodbye."
            echo "0" > "$CRASH_FILE"
            break
            ;;
        *)
            # Unexpected crash
            log "Daemon exited with code $EXIT_CODE. Cooling down (${COOLDOWN}s)..."
            sleep "$COOLDOWN"
            ;;
    esac
done
