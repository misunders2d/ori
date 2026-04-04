#!/usr/bin/env bash
# Ori — Smart start script.
#   1. If systemd service exists → starts/restarts it
#   2. If systemd available but not installed → installs it
#   3. Otherwise → runs supervisor directly in foreground
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="$PROJECT_ROOT/.venv/bin/python"
SUPERVISOR="$PROJECT_ROOT/deploy/ori-supervisor.py"

# Ensure venv exists
if [ ! -f "$PYTHON" ]; then
    echo ":: Setting up Python virtual environment..."
    if command -v uv &>/dev/null; then
        uv sync
    else
        python3 -m venv .venv
        .venv/bin/pip install -e .
    fi
fi

# Derive service name
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ]; then
    _env_name=$(python3 -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
elif [ -f "$PROJECT_ROOT/data/.env" ]; then
    _env_name=$(grep -v '^#' "$PROJECT_ROOT/data/.env" | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d "\"'\\r" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# Run setup wizard if no LLM provider configured
needs_setup() {
    if [ -f "$VAULT_FILE" ]; then
        python3 -c "import json; d=json.load(open('$VAULT_FILE')); exit(0 if any(d.get(k) for k in ['GOOGLE_API_KEY','ANTHROPIC_API_KEY']) or d.get('GOOGLE_GENAI_USE_VERTEXAI','').upper()=='TRUE' else 1)" 2>/dev/null && return 1
    fi
    [ -f "data/.env" ] && grep -qE '(GOOGLE_API_KEY|ANTHROPIC_API_KEY)=.+' "data/.env" 2>/dev/null && return 1
    return 0
}

if needs_setup; then
    echo ":: No LLM provider configured. Running setup wizard..."
    if command -v python3 &>/dev/null; then
        python3 interfaces/setup_wizard.py
    else
        echo ":: Error: Python not found."
        exit 1
    fi
fi

if command -v systemctl &>/dev/null; then
    if systemctl cat "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        echo ":: Restarting $SERVICE_NAME via systemd..."
        sudo systemctl restart "$SERVICE_NAME"
        echo ":: $SERVICE_NAME is running."
        echo "   Logs:  sudo journalctl -u $SERVICE_NAME -f"
        echo "   Stop:  sudo systemctl stop $SERVICE_NAME"
    else
        echo ":: First run detected. Installing $SERVICE_NAME..."
        "$SCRIPT_DIR/install.sh"
    fi
else
    # No systemd — run supervisor directly
    echo ":: Starting $SERVICE_NAME (foreground)..."
    exec "$PYTHON" "$SUPERVISOR"
fi
