#!/usr/bin/env bash
# ============================================================================
# Ori — One-command start. No sudo. No root. No manual steps.
#
# What happens:
#   1. Checks Python and sets up venv if needed
#   2. Runs setup wizard if no LLM keys configured (first run)
#   3. Installs as a user-level service (systemd/launchd) on first run
#   4. Restarts the service on subsequent runs
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ---- Step 1: Check prerequisites ----
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "  ERROR: Python 3 is required but not installed."
    echo ""
    echo "  Install it:"
    echo "    Ubuntu/Debian:  sudo apt install python3 python3-venv"
    echo "    macOS:          brew install python3"
    echo "    Arch:           sudo pacman -S python"
    echo ""
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$(python3 -c 'import sys; print(sys.version_info.major)')" -lt 3 ] || [ "$PY_MINOR" -lt 10 ]; then
    echo ""
    echo "  ERROR: Python 3.10+ required (found $PY_VERSION)."
    echo ""
    exit 1
fi

# ---- Step 2: Ensure venv exists ----
if [ ! -f ".venv/bin/python" ]; then
    echo ":: Setting up Python environment..."
    if command -v uv &>/dev/null; then
        uv sync
    else
        echo ":: Installing uv package manager..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
        uv sync
    fi
    echo ":: Python environment ready."
fi

PYTHON="$PROJECT_ROOT/.venv/bin/python"
SUPERVISOR="$PROJECT_ROOT/deploy/ori-supervisor.py"

# ---- Step 3: Run setup wizard if needed ----
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
        echo ""
        echo ":: First-time setup — let's configure your agent."
        echo ""
        "$PYTHON" app/transports/setup_wizard.py
    else
        echo ""
        echo "  ERROR: First-time setup requires an interactive terminal."
        echo "  Run this command from a terminal window:"
        echo ""
        echo "    cd $PROJECT_ROOT && deploy/start.sh"
        echo ""
        exit 1
    fi
fi

# ---- Step 4: Derive service name ----
_bot_name="ori"
if [ -f "$VAULT_FILE" ]; then
    _env_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# ---- Step 5: Start ----
is_service_installed() {
    OS="$(uname -s)"
    case "$OS" in
        Linux)
            systemctl --user cat "$SERVICE_NAME" &>/dev/null 2>&1
            ;;
        Darwin)
            [ -f "$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist" ]
            ;;
        *)
            return 1
            ;;
    esac
}

if is_service_installed; then
    OS="$(uname -s)"
    case "$OS" in
        Linux)  systemctl --user restart "$SERVICE_NAME" ;;
        Darwin)
            plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
            launchctl unload "$plist" 2>/dev/null || true
            launchctl load "$plist"
            ;;
    esac
    echo ":: $SERVICE_NAME restarted."

    # Restart Cloudflare tunnel with a fresh URL
    if command -v docker &>/dev/null; then
        _a2a_port=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('A2A_PORT','8000'))" 2>/dev/null || echo "8000")
        _tunnel_name="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')"
        _metrics_port=$("$PYTHON" -c "import json; v=json.load(open('$VAULT_FILE')); print(v.get('TUNNEL_METRICS_PORT', int(v.get('A2A_PORT','8000'))+1000))" 2>/dev/null || echo "9000")
        # Kill any existing tunnel container for this bot (by name, regardless of how it was created)
        docker rm -f "${_tunnel_name}-tunnel" 2>/dev/null || true
        # Start fresh tunnel scoped by bot name
        A2A_PORT="$_a2a_port" BOT_NAME="$_tunnel_name" TUNNEL_METRICS_PORT="$_metrics_port" \
            docker compose -p "$_tunnel_name" -f "$SCRIPT_DIR/docker-compose.yml" up -d || \
            echo "   Warning: Cloudflare tunnel failed to start. A2A will not be reachable externally."
    fi

    echo "   Logs:  deploy/logs.sh"
    echo "   Stop:  deploy/stop.sh"
else
    echo ":: Installing $SERVICE_NAME as a background service..."
    "$SCRIPT_DIR/install.sh"
fi
