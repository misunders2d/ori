#!/usr/bin/env bash
# ============================================================================
# Ori Installer — Sets up the agent as a user-level system service.
# No sudo required. No root access needed.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="$PROJECT_ROOT/.venv/bin/python"
SUPERVISOR="$PROJECT_ROOT/deploy/ori-supervisor.py"

# Derive service name
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ]; then
    _env_name=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# ---- Linux (user-level systemd) ----
install_systemd() {
    local service_dir="$HOME/.config/systemd/user"
    local service_file="$service_dir/${SERVICE_NAME}.service"
    mkdir -p "$service_dir"

    cat > "$service_file" <<UNIT
[Unit]
Description=${SERVICE_NAME} daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON $SUPERVISOR
Restart=always
RestartPreventExitStatus=0
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=default.target
UNIT

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    # Record the service name so uninstall can find it even if BOT_NAME changes
    echo "$SERVICE_NAME" > "$PROJECT_ROOT/data/.service_name"

    # Enable lingering so service starts on boot even without login
    loginctl enable-linger "$(whoami)" 2>/dev/null || true

    echo ""
    echo ":: $SERVICE_NAME installed and running."
    echo "   Logs:  journalctl --user -u $SERVICE_NAME -f"
    echo "   Stop:  deploy/stop.sh"
}

# ---- macOS (launchd) ----
install_launchd() {
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

    # Record the service name so uninstall can find it even if BOT_NAME changes
    echo "$SERVICE_NAME" > "$PROJECT_ROOT/data/.service_name"

    echo ""
    echo ":: $SERVICE_NAME installed and running."
    echo "   Logs:  tail -f $log_dir/stdout.log"
    echo "   Stop:  deploy/stop.sh"
}

# ---- Main ----
echo ":: Installing $SERVICE_NAME..."

OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            install_systemd
        else
            echo ":: No systemd found. Running in foreground instead."
            exec "$PYTHON" "$SUPERVISOR"
        fi
        ;;
    Darwin)
        install_launchd
        ;;
    *)
        echo ":: Unsupported platform ($OS). Running in foreground instead."
        exec "$PYTHON" "$SUPERVISOR"
        ;;
esac

# Start tunnel if Docker is available
if command -v docker &>/dev/null; then
    # Read A2A_PORT from vault so the tunnel points to the right port
    _a2a_port=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('A2A_PORT','8000'))" 2>/dev/null || echo "8000")
    echo ":: Starting Cloudflare tunnel (port $_a2a_port)..."
    A2A_PORT="$_a2a_port" docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d 2>/dev/null || true
else
    echo ""
    echo "   Note: Docker not found. The Cloudflare tunnel (for A2A internet access)"
    echo "   requires Docker. Install Docker if you need internet-facing A2A communication."
fi

# ---- Post-install health check ----
echo ""
echo ":: Waiting for $SERVICE_NAME to start..."
_healthy=false
for i in $(seq 1 15); do
    sleep 2
    if curl -sf http://localhost:8000/.well-known/agent-card.json &>/dev/null; then
        _healthy=true
        break
    fi
done

BOT_NAME=$("$PYTHON" -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME','Ori'))" 2>/dev/null || echo "Ori")

echo ""
echo "============================================"
if $_healthy; then
    echo "  $BOT_NAME is live and ready!"
else
    echo "  $BOT_NAME is starting up (may take a moment)."
fi
echo ""
echo "  Commands:"
echo "    deploy/start.sh   — start/restart"
echo "    deploy/stop.sh    — stop"
echo "    deploy/logs.sh    — view logs"
echo "============================================"
echo ""
