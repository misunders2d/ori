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
if any(d.get(k) for k in ['GOOGLE_API_KEY','ANTHROPIC_API_KEY','OPENROUTER_API_KEY']) or d.get('GOOGLE_GENAI_USE_VERTEXAI','').upper()=='TRUE':
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

# ---- Step 4: Derive service name + auto-pick port ----
#
# Worktree-friendly defaults (so multiple checkouts of the same repo can
# run in parallel without manual config). If vault has no BOT_NAME, derive
# from the worktree directory: `main/` → "Ori" (canonical), anything else
# uses the dir name capitalized — e.g. `amazon_manager/` → "Amazon-Manager".
# Persist to vault on first run so the choice is stable.
_bot_name=""
if [ -f "$VAULT_FILE" ]; then
    _bot_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
fi
if [ -z "$_bot_name" ]; then
    _basename=$(basename "$PROJECT_ROOT")
    case "$_basename" in
        main|ori|Ori) _bot_name="Ori" ;;
        *)
            # Title-case dashes/underscores: amazon_manager → Amazon-Manager
            _bot_name=$(echo "$_basename" | tr '_' '-' | awk -F- 'BEGIN{OFS="-"} {for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) tolower(substr($i,2))} 1')
            ;;
    esac
    "$PYTHON" -c "from deploy.vault import set as _s; _s('BOT_NAME','$_bot_name')" 2>/dev/null || true
    echo ":: Auto-assigned BOT_NAME=$_bot_name (derived from worktree)."
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# Auto-pick A2A_PORT if vault has none, so the tunnel starts on the right
# port from t=0 (rather than waiting for supervisor to pick + refresh).
_a2a_port_vault=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('A2A_PORT',''))" 2>/dev/null || true)
if [ -z "$_a2a_port_vault" ]; then
    _a2a_port=$("$PYTHON" -c "
import socket
p = 8000
for _ in range(100):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', p)) != 0:
            break
    p += 1
print(p)
" 2>/dev/null || echo "8000")
    "$PYTHON" -c "from deploy.vault import set as _s; _s('A2A_PORT','$_a2a_port')" 2>/dev/null || true
    echo ":: Auto-assigned A2A_PORT=$_a2a_port (first free port from 8000)."
fi

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
        Linux)
            # Re-enable in case stop.sh disabled it. Symmetric pair:
            # stop.sh = stop+disable, start.sh = enable+start.
            if ! systemctl --user is-enabled "$SERVICE_NAME" &>/dev/null; then
                systemctl --user enable "$SERVICE_NAME"
                echo ":: $SERVICE_NAME enabled — will auto-start on reboot."
            fi
            systemctl --user restart "$SERVICE_NAME"
            ;;
        Darwin)
            plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
            # `load -w` clears any persistent Disabled flag and loads.
            launchctl unload "$plist" 2>/dev/null || true
            launchctl load -w "$plist"
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
