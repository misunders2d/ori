#!/usr/bin/env bash
# ============================================================================
# Ori Installer — One-time auto-start setup
# ============================================================================
# Sets up the Python supervisor as a system service.
# Docker is only needed for the Cloudflare tunnel and child agents.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Ensure venv and uv exist
if [ ! -f "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo ":: Setting up Python virtual environment..."
    cd "$PROJECT_ROOT"
    if command -v uv &>/dev/null; then
        uv sync
    else
        python3 -m venv .venv
        .venv/bin/pip install -e .
    fi
fi

PYTHON="$PROJECT_ROOT/.venv/bin/python"
SUPERVISOR="$PROJECT_ROOT/deploy/ori-supervisor.py"

# Derive service name from vault or default
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

# --- Detect platform ---
install_systemd() {
    echo ":: Installing systemd service..."

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    sudo tee "$service_file" > /dev/null <<UNIT
[Unit]
Description=${SERVICE_NAME} daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON $SUPERVISOR
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"

    echo ""
    echo ":: $SERVICE_NAME installed and running."
    echo "   Status:  sudo systemctl status $SERVICE_NAME"
    echo "   Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo "   Stop:    sudo systemctl stop $SERVICE_NAME"
}

install_launchd() {
    echo ":: Installing launchd service..."

    local plist_dir="$HOME/Library/LaunchAgents"
    local plist_file="$plist_dir/com.${SERVICE_NAME}.plist"
    local log_dir="$HOME/Library/Logs/${SERVICE_NAME}"
    mkdir -p "$plist_dir" "$log_dir"

    cat > "$plist_file" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.${SERVICE_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SUPERVISOR</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$log_dir/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$log_dir/stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST

    launchctl load "$plist_file"

    echo ""
    echo ":: $SERVICE_NAME installed and running."
    echo "   Logs:    tail -f $log_dir/stdout.log"
    echo "   Stop:    launchctl unload $plist_file"
}

# --- Main ---
echo ":: Ori Installer ($SERVICE_NAME)"
echo "   Project: $PROJECT_ROOT"
echo ""

OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            install_systemd
        else
            echo "Error: systemd not found. Run the supervisor manually:"
            echo "  $PYTHON $SUPERVISOR"
            exit 1
        fi
        ;;
    Darwin)
        install_launchd
        ;;
    *)
        echo "Unsupported platform: $OS"
        echo "Run the supervisor manually: $PYTHON $SUPERVISOR"
        exit 1
        ;;
esac

# Start the tunnel if Docker is available
if command -v docker &>/dev/null; then
    echo ""
    echo ":: Starting Cloudflare tunnel..."
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d 2>/dev/null || true
fi
