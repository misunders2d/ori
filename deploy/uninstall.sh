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
OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
                systemctl --user stop "$SERVICE_NAME"
                echo ":: Stopped $SERVICE_NAME service."
            fi
            if systemctl --user is-enabled "$SERVICE_NAME" &>/dev/null; then
                systemctl --user disable "$SERVICE_NAME"
            fi
            SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
            if [ -f "$SERVICE_FILE" ]; then
                rm "$SERVICE_FILE"
                systemctl --user daemon-reload
                echo ":: Removed systemd unit file."
            fi
        fi
        ;;
    Darwin)
        PLIST="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
        if [ -f "$PLIST" ]; then
            launchctl unload "$PLIST" 2>/dev/null || true
            rm "$PLIST"
            echo ":: Removed launchd plist."
        fi
        LOG_DIR="$HOME/Library/Logs/${SERVICE_NAME}"
        if [ -d "$LOG_DIR" ]; then
            rm -rf "$LOG_DIR"
            echo ":: Removed launchd log directory."
        fi
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
