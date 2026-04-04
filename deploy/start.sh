#!/usr/bin/env bash
# Ori — One-command start.
#   - Ensures venv exists
#   - Runs setup wizard if no LLM provider configured (interactive)
#   - Installs systemd/launchd service on first run
#   - Restarts service on subsequent runs
#   - Falls back to foreground mode if no service manager
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# --- 1. Ensure Python venv exists ---
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

# --- 2. Run setup wizard if no LLM provider configured ---
# This MUST happen here (interactive terminal) before systemd takes over.
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"

needs_setup() {
    if [ -f "$VAULT_FILE" ]; then
        "$PYTHON" -c "
import json, sys
d = json.load(open('$VAULT_FILE'))
if any(d.get(k) for k in ['GOOGLE_API_KEY','ANTHROPIC_API_KEY']) or d.get('GOOGLE_GENAI_USE_VERTEXAI','').upper()=='TRUE':
    sys.exit(0)
sys.exit(1)
" 2>/dev/null && return 1
    fi
    return 0
}

if needs_setup; then
    if [ -t 0 ]; then
        echo ":: No LLM provider configured. Running setup wizard..."
        "$PYTHON" interfaces/setup_wizard.py
    else
        echo ":: Error: No LLM provider configured and no interactive terminal."
        echo "   Run this script from an interactive terminal first to complete setup."
        exit 1
    fi
fi

# --- 3. Derive service name ---
_bot_name="ori"
if [ -f "$VAULT_FILE" ]; then
    _env_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# --- 4. Start ---
if command -v systemctl &>/dev/null; then
    if systemctl cat "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        echo ":: Restarting $SERVICE_NAME via systemd..."
        sudo systemctl restart "$SERVICE_NAME"
    else
        echo ":: First run — installing $SERVICE_NAME as a system service..."
        "$SCRIPT_DIR/install.sh"
    fi
    echo ":: $SERVICE_NAME is running."
    echo "   Logs:  sudo journalctl -u $SERVICE_NAME -f"
    echo "   Stop:  sudo systemctl stop $SERVICE_NAME"
else
    echo ":: Starting $SERVICE_NAME (foreground, Ctrl+C to stop)..."
    exec "$PYTHON" "$SUPERVISOR"
fi
