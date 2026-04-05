#!/usr/bin/env bash
# Ori — View agent logs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

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
        journalctl --user -u "$SERVICE_NAME" -f --no-hostname
        ;;
    Darwin)
        tail -f "$HOME/Library/Logs/$SERVICE_NAME/stdout.log"
        ;;
    *)
        tail -f "$PROJECT_ROOT/data/agent.log"
        ;;
esac
