#!/usr/bin/env bash
# ============================================================================
# Ori Uninstall — Completely remove an installed instance.
#
# Removes:
#   - System service (systemd / launchd)
#   - Cloudflare tunnel container
#   - All spawned child containers and images
#   - The project directory itself
#
# Usage:
#   deploy/uninstall.sh          # interactive confirmation
#   deploy/uninstall.sh --yes    # skip confirmation
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

SKIP_CONFIRM=false
[[ "${1:-}" == "--yes" ]] && SKIP_CONFIRM=true

# ---- Derive instance identity ----
PYTHON="$PROJECT_ROOT/.venv/bin/python"
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ] && [ -x "$PYTHON" ]; then
    _env_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SAFE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')"
SERVICE_NAME="${SAFE_NAME}-agent"
INSTANCE_PREFIX="$SAFE_NAME"

# Also read the originally-installed service name (may differ if BOT_NAME changed)
ORIGINAL_SERVICE_NAME=""
if [ -f "$PROJECT_ROOT/data/.service_name" ]; then
    ORIGINAL_SERVICE_NAME=$(cat "$PROJECT_ROOT/data/.service_name" 2>/dev/null || true)
fi

echo ""
echo "============================================"
echo "  Uninstalling: $_bot_name"
echo "  Service:      $SERVICE_NAME"
echo "  Directory:    $PROJECT_ROOT"
echo "============================================"
echo ""

if ! $SKIP_CONFIRM; then
    echo "  This will permanently remove:"
    echo "    - The $SERVICE_NAME system service"
    echo "    - All spawned child containers and images"
    echo "    - The Cloudflare tunnel container"
    echo "    - The entire project directory ($PROJECT_ROOT)"
    echo ""
    read -rp "  Type 'yes' to confirm: " answer
    if [[ "$answer" != "yes" ]]; then
        echo "  Aborted."
        exit 0
    fi
    echo ""
fi

# ---- 1. Stop and remove system service ----
# Collect all candidate service names (current + original + any that reference this directory)
_remove_systemd_service() {
    local svc="$1"
    [ -z "$svc" ] && return
    systemctl --user stop "$svc" 2>/dev/null || true
    systemctl --user disable "$svc" 2>/dev/null || true
    local svc_file="$HOME/.config/systemd/user/${svc}.service"
    if [ -f "$svc_file" ]; then
        rm "$svc_file"
        echo ":: Removed systemd service: $svc"
    fi
}

OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            _remove_systemd_service "$SERVICE_NAME"
            # Also remove the originally-installed service if name differs
            if [ -n "$ORIGINAL_SERVICE_NAME" ] && [ "$ORIGINAL_SERVICE_NAME" != "$SERVICE_NAME" ]; then
                _remove_systemd_service "$ORIGINAL_SERVICE_NAME"
            fi
            # Scan for any other service files referencing this project directory
            for f in "$HOME/.config/systemd/user/"*-agent.service; do
                [ -f "$f" ] || continue
                if grep -q "$PROJECT_ROOT" "$f" 2>/dev/null; then
                    svc_name="$(basename "$f" .service)"
                    if [ "$svc_name" != "$SERVICE_NAME" ] && [ "$svc_name" != "$ORIGINAL_SERVICE_NAME" ]; then
                        _remove_systemd_service "$svc_name"
                    fi
                fi
            done
            systemctl --user daemon-reload
        fi
        ;;
    Darwin)
        # Remove current + original plist
        for _svc in "$SERVICE_NAME" "$ORIGINAL_SERVICE_NAME"; do
            [ -z "$_svc" ] && continue
            PLIST="$HOME/Library/LaunchAgents/com.${_svc}.plist"
            if [ -f "$PLIST" ]; then
                launchctl unload "$PLIST" 2>/dev/null || true
                rm "$PLIST"
                echo ":: Removed launchd plist: $_svc"
            fi
            LOG_DIR="$HOME/Library/Logs/${_svc}"
            if [ -d "$LOG_DIR" ]; then
                rm -rf "$LOG_DIR"
            fi
        done
        ;;
esac

# ---- 2. Stop and remove spawned child containers ----
if command -v docker &>/dev/null; then
    # Find all child containers belonging to this instance
    CHILDREN=$(docker ps -a --filter "label=ori.parent=${INSTANCE_PREFIX}" --format "{{.Names}}" 2>/dev/null || true)
    if [ -n "$CHILDREN" ]; then
        echo ":: Stopping child containers..."
        echo "$CHILDREN" | xargs docker stop 2>/dev/null || true
        echo "$CHILDREN" | xargs docker rm 2>/dev/null || true
        echo ":: Removed child containers: $CHILDREN"
    fi

    # Remove the child image
    CHILD_IMAGE="${INSTANCE_PREFIX}-child-image"
    if docker images -q "$CHILD_IMAGE" 2>/dev/null | grep -q .; then
        docker rmi "$CHILD_IMAGE" 2>/dev/null || true
        echo ":: Removed child image: $CHILD_IMAGE"
    fi

    # Stop Cloudflare tunnel
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true
    echo ":: Stopped Cloudflare tunnel."

    # Prune dangling images from this instance
    docker image prune -f --filter "label=ori.instance=${INSTANCE_PREFIX}" 2>/dev/null || true
fi

# ---- 3. Remove project directory ----
echo ":: Removing $PROJECT_ROOT..."
cd /
rm -rf "$PROJECT_ROOT"
echo ":: Directory removed."

echo ""
echo "============================================"
echo "  $_bot_name has been fully uninstalled."
echo "============================================"
echo ""
