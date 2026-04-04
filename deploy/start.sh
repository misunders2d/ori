#!/usr/bin/env bash
# Ori — One-command start.
#   - First run: installs venv, runs setup wizard, installs systemd service
#   - Subsequent runs: restarts the service (or runs foreground if no systemd)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# --- Ensure Python venv exists ---
if [ ! -f ".venv/bin/python" ]; then
    echo ":: Setting up Python virtual environment..."
    if command -v uv &>/dev/null; then
        uv sync
    elif command -v python3 &>/dev/null; then
        python3 -m venv .venv
        .venv/bin/pip install -e .
    else
        echo ":: Error: Python 3 not found. Install Python 3.10+ first."
        exit 1
    fi
fi

PYTHON="$PROJECT_ROOT/.venv/bin/python"
SUPERVISOR="$PROJECT_ROOT/deploy/ori-supervisor.py"

# --- Derive service name from vault or default ---
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ]; then
    _env_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# --- Start ---
if command -v systemctl &>/dev/null; then
    if systemctl cat "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        echo ":: Restarting $SERVICE_NAME via systemd..."
        sudo systemctl restart "$SERVICE_NAME"
        echo ":: $SERVICE_NAME is running."
        echo "   Logs:  sudo journalctl -u $SERVICE_NAME -f"
        echo "   Stop:  sudo systemctl stop $SERVICE_NAME"
    else
        echo ":: First run — installing $SERVICE_NAME as a system service..."
        "$SCRIPT_DIR/install.sh"
    fi
else
    echo ":: Starting $SERVICE_NAME (foreground, Ctrl+C to stop)..."
    exec "$PYTHON" "$SUPERVISOR"
fi
