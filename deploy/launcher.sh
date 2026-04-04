#!/usr/bin/env bash
# ============================================================================
# Ori Launcher — Platform-aware recovery supervisor
# ============================================================================
# This script is the ONLY thing the user runs. It owns all recovery logic.
# It must NEVER be modified by Ori's self-evolution (DeveloperAgent).
#
# Recovery chain:
#   systemd/launchd (auto-start) -> launcher.sh (rollback) -> docker compose -f deploy/docker-compose.yml (the bot)
#
# Supports: Linux, macOS, Windows (WSL)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# --- Configuration ---
IMAGE_NAME=""  # set after BOT_NAME is read
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
BOT_NAME_LOWER="${BOT_NAME,,}"  # lowercase for Docker naming
export BOT_NAME BOT_NAME_LOWER  # docker-compose.yml uses these
IMAGE_NAME="${BOT_NAME_LOWER}-agent-image"
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
        if ! docker compose -f deploy/docker-compose.yml build --no-cache; then
            log "ERROR: Full rebuild failed!"
            return 1
        fi
    else
        log "Code-only change. Incremental rebuild..."
        if ! docker compose -f deploy/docker-compose.yml build; then
            log "ERROR: Incremental rebuild failed!"
            return 1
        fi
    fi
    touch data/.last_build
    # Always prune dangling images after build
    docker image prune -f 2>/dev/null || true
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

# --- Ensure current user can access Docker without sudo ---
if [ -S /var/run/docker.sock ] && ! docker info &>/dev/null; then
    log "Docker socket not accessible. Adding $(whoami) to the docker group..."
    if command -v sudo &>/dev/null; then
        sudo groupadd -f docker 2>/dev/null || true
        sudo usermod -aG docker "$(whoami)"
        log "Added to docker group. Activating new group membership..."
        # Re-exec this script under the new group so the rest of the session works
        exec sg docker "$0"
    else
        log "ERROR: Cannot access Docker and sudo is not available."
        log "Run manually: sudo usermod -aG docker $(whoami) && newgrp docker"
        exit 1
    fi
fi

# --- Ensure HOME is set (may be missing under some init systems) ---
export HOME="${HOME:-$(eval echo ~$(whoami))}"

# --- Copy ADC credentials into data/ so the container can read them ---
# gcloud auth saves credentials to ~/.config/gcloud/ on the host.
# We copy the file into data/ (already mounted) instead of mounting gcloud directly,
# which avoids: permission issues, creating directories for non-existent files,
# and breaking gcloud on the host.
ADC_HOST="${HOME}/.config/gcloud/application_default_credentials.json"
ADC_DATA="data/.adc.json"
if [ -f "$ADC_HOST" ]; then
    cp "$ADC_HOST" "$ADC_DATA" 2>/dev/null || true
    chmod 644 "$ADC_DATA" 2>/dev/null || true
    log "ADC credentials copied to data/.adc.json"
fi

# --- Generate secrets if missing (BEFORE container starts) ---
# These are written to .env ONCE and never again. The running container
# treats .env as read-only.
generate_secret() {
    local key="$1" prefix="${2:-}"
    if [ -f "data/.env" ] && grep -qE "^${key}=.+" "data/.env" 2>/dev/null; then
        return  # already set
    fi
    local value="${prefix}$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))' 2>/dev/null || head -c32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c32)"
    echo "${key}=${value}" >> "data/.env"
    log "Generated ${key}"
}

# Ensure data/.env exists (may be missing on first run or after disaster recovery)
mkdir -p data
touch data/.env
generate_secret "ADMIN_PASSCODE"
generate_secret "A2A_API_KEY" "ori-"

# --- First-time setup wizard ---
# Require at least one LLM provider to be configured before starting.
needs_setup() {
    [ ! -f "data/.env" ] && return 0
    # Check for any provider: API key or Vertex AI mode (handles quoted values)
    grep -qE 'GOOGLE_API_KEY=.+' "data/.env" 2>/dev/null && return 1
    grep -qE 'ANTHROPIC_API_KEY=.+' "data/.env" 2>/dev/null && return 1
    grep -qiE 'GOOGLE_GENAI_USE_VERTEXAI=.?TRUE' "data/.env" 2>/dev/null && return 1
    return 0
}

if needs_setup; then
    # Only run wizard if we have an interactive terminal
    if [ -t 0 ]; then
        log "No LLM provider configured. Launching setup wizard..."
        if command -v python3 &>/dev/null; then
            python3 interfaces/setup_wizard.py
        elif command -v python &>/dev/null; then
            python interfaces/setup_wizard.py
        else
            log "No host Python found. Please run: python3 interfaces/setup_wizard.py"
            exit 1
        fi
    else
        log "ERROR: No LLM provider configured and no interactive terminal available."
        log "Run the setup wizard manually: python3 interfaces/setup_wizard.py"
        exit 1
    fi
fi

# ============================================================================
# THE REGENERATION LOOP
# ============================================================================
log "Starting regeneration loop..."

# Fingerprint of this script at startup — used to detect on-disk changes
_SELF_HASH=$(sha256sum "${BASH_SOURCE[0]}" 2>/dev/null | cut -d' ' -f1)

while true; do
    # --- HOT-RELOAD: re-exec if launcher.sh changed on disk ---
    _CURRENT_HASH=$(sha256sum "${BASH_SOURCE[0]}" 2>/dev/null | cut -d' ' -f1)
    if [ "$_CURRENT_HASH" != "$_SELF_HASH" ]; then
        log "Launcher script changed on disk. Re-executing..."
        exec "${BASH_SOURCE[0]}"
    fi

    CRASHES=$(read_crashes)
    BOT_NAME=$(read_bot_name)  # re-read in case .env changed
    BOT_NAME_LOWER="${BOT_NAME,,}"
    export BOT_NAME BOT_NAME_LOWER
    IMAGE_NAME="${BOT_NAME_LOWER}-agent-image"

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
        BUILD_FLAG="yes"
    fi

    # --- LAUNCH ---
    CONTAINER_NAME="${BOT_NAME_LOWER}-agent"
    if is_interactive; then
        log "No messenger configured. Launching interactive CLI..."
        docker compose -f deploy/docker-compose.yml up -d cloudflare-tunnel 2>/dev/null || true
        docker compose -f deploy/docker-compose.yml run --rm -it --service-ports agent
        EXIT_CODE=$?
    else
        # Build if needed BEFORE launching (--build inside `up` can mask failures)
        if [ -n "$BUILD_FLAG" ]; then
            smart_build || { log "ERROR: Build failed. Cooling down..."; sleep "$COOLDOWN"; continue; }
        fi

        # Start all services detached so the launcher retains control
        docker compose -f deploy/docker-compose.yml up -d

        # Background stability check: if boot survives STABLE_THRESHOLD, reset crash counter
        (
            sleep "$STABLE_THRESHOLD"
            if docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
                log "Boot stable for ${STABLE_THRESHOLD}s. Resetting crash counter."
                echo "0" > "$CRASH_FILE"
            fi
        ) &
        STABILITY_PID=$!

        # Wait for the AGENT container specifically (not compose, not tunnel)
        # docker wait blocks until the container stops and returns its exit code
        EXIT_CODE=$(docker wait "$CONTAINER_NAME" 2>/dev/null || echo "1")
        kill "$STABILITY_PID" 2>/dev/null || true

        # Stop compose so we get a clean restart on the next loop iteration
        docker compose -f deploy/docker-compose.yml down --timeout 5 2>/dev/null || true
    fi

    touch data/.last_build

    # --- SIGNAL HANDLING ---
    # The container writes its intent to data/.exit_signal and exits with 0
    # so Docker does not race-restart it. Read the signal file first.
    SIGNAL_FILE="data/.exit_signal"
    if [ -f "$SIGNAL_FILE" ]; then
        EXIT_CODE=$(cat "$SIGNAL_FILE" 2>/dev/null || echo "0")
        rm -f "$SIGNAL_FILE"
    fi

    case $EXIT_CODE in
        100)
            # Evolution signal: Ori committed changes, sync and rebuild
            log "Evolution signal (100). Syncing and rebuilding..."
            # Snapshot .env BEFORE any git operations
            [ -f "data/.env" ] && cp -a data/.env data/.env.pre-signal 2>/dev/null || true
            # Pull from remote if configured, otherwise changes are already local
            if git remote get-url origin &>/dev/null; then
                git fetch origin master 2>/dev/null || true
                git reset --hard origin/master
                git clean -fd --exclude=data --exclude='data/*' --exclude=.env
            fi
            # Restore .env if git operations damaged it
            if [ -f "data/.env.pre-signal" ]; then
                pre_keys=$(grep -cve '^\s*#' -e '^\s*$' data/.env.pre-signal 2>/dev/null || echo 0)
                cur_keys=$(grep -cve '^\s*#' -e '^\s*$' data/.env 2>/dev/null || echo 0)
                if [ "$pre_keys" -gt "$cur_keys" ]; then
                    cp -a data/.env.pre-signal data/.env
                    log "Restored .env from pre-signal snapshot ($pre_keys keys vs $cur_keys)."
                fi
                rm -f data/.env.pre-signal
            fi
            smart_build || log "WARNING: Build failed, starting with existing image."
            echo "0" > "$CRASH_FILE"
            ;;
        101)
            # Rollback signal: Ori requested manual rollback
            log "Rollback signal (101). Reverting to previous commit..."
            [ -f "data/.env" ] && cp -a data/.env data/.env.pre-signal 2>/dev/null || true
            git reset --hard HEAD~1
            git clean -fd --exclude=data --exclude='data/*' --exclude=.env
            # Restore .env if git operations damaged it
            if [ -f "data/.env.pre-signal" ]; then
                pre_keys=$(grep -cve '^\s*#' -e '^\s*$' data/.env.pre-signal 2>/dev/null || echo 0)
                cur_keys=$(grep -cve '^\s*#' -e '^\s*$' data/.env 2>/dev/null || echo 0)
                if [ "$pre_keys" -gt "$cur_keys" ]; then
                    cp -a data/.env.pre-signal data/.env
                    log "Restored .env from pre-signal snapshot."
                fi
                rm -f data/.env.pre-signal
            fi
            smart_build || log "WARNING: Build failed, starting with existing image."
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
