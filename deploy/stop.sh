#!/usr/bin/env bash
# Ori — Stop the agent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Derive service name
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ] && command -v python3 &>/dev/null; then
    _env_name=$(python3 -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

OS="$(uname -s)"
case "$OS" in
    Linux)
        if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
            systemctl --user stop "$SERVICE_NAME"
            echo ":: $SERVICE_NAME stopped."
        else
            echo ":: $SERVICE_NAME is not running."
        fi
        ;;
    Darwin)
        plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
        if [ -f "$plist" ]; then
            launchctl unload "$plist" 2>/dev/null || true
            echo ":: $SERVICE_NAME stopped."
        else
            echo ":: $SERVICE_NAME is not running."
        fi
        ;;
esac

# Stop tunnel
if command -v docker &>/dev/null; then
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true
fi
