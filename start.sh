#!/usr/bin/env bash
# Ori — Smart start script. Does the right thing automatically:
#   1. If systemd is available and service isn't installed → installs it
#   2. If systemd service exists → starts/restarts it
#   3. Otherwise → runs launcher.sh in background with logging
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Derive service name from BOT_NAME in .env, default to "ori"
_bot_name="ori"
if [ -f "data/.env" ]; then
    _env_name=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d "\"'\\r" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# --- Run setup wizard if no provider configured (needs interactive terminal) ---
needs_setup() {
    [ ! -f "data/.env" ] && return 0
    grep -qE 'GOOGLE_API_KEY=.+' "data/.env" 2>/dev/null && return 1
    grep -qE 'ANTHROPIC_API_KEY=.+' "data/.env" 2>/dev/null && return 1
    grep -qiE 'GOOGLE_GENAI_USE_VERTEXAI=.?TRUE' "data/.env" 2>/dev/null && return 1
    return 0
}

if needs_setup; then
    echo ":: No LLM provider configured. Running setup wizard..."
    if command -v python3 &>/dev/null; then
        python3 interfaces/setup_wizard.py
    elif command -v python &>/dev/null; then
        python interfaces/setup_wizard.py
    else
        echo ":: Error: Python not found. Please run the setup wizard manually."
        exit 1
    fi
fi

if command -v systemctl &>/dev/null; then
    # systemd available
    if systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null && \
       systemctl cat "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        # Service already installed — restart it
        echo ":: Restarting $SERVICE_NAME via systemd..."
        sudo systemctl restart "$SERVICE_NAME"
        echo ":: $SERVICE_NAME is running."
        echo "   Logs:  sudo journalctl -u $SERVICE_NAME -f"
        echo "   Stop:  sudo systemctl stop $SERVICE_NAME"
    else
        # systemd available but service not installed — install it
        echo ":: First run detected. Installing $SERVICE_NAME as a system service..."
        ./install.sh
    fi
else
    # No systemd — run launcher in background
    echo ":: Starting $SERVICE_NAME in background..."
    mkdir -p data
    nohup ./launcher.sh >> data/launcher.log 2>&1 &
    echo ":: $SERVICE_NAME is running (PID: $!)."
    echo "   Logs:  tail -f data/launcher.log"
    echo "   Stop:  kill $!"
fi
