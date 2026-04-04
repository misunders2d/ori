#!/usr/bin/env bash
# ============================================================================
# Ori Installer — One-time auto-start setup
# ============================================================================
# Run this once on a fresh server. After that, Ori is self-sustaining.
#
# Supports:
#   - Linux (systemd)
#   - macOS (launchd)
#   - WSL (treated as Linux)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCHER_PATH="$SCRIPT_DIR/launcher.sh"

# Derive service name from BOT_NAME in .env, default to "ori"
_bot_name="ori"
if [ -f "$PROJECT_ROOT/data/.env" ]; then
    _env_name=$(grep -v '^#' "$PROJECT_ROOT/data/.env" | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d "\"'\\r" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
# Sanitize for systemd: lowercase, spaces/underscores to hyphens, strip non-alnum
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# Ensure launcher exists and is executable
if [ ! -f "$LAUNCHER_PATH" ]; then
    echo "Error: launcher.sh not found at $LAUNCHER_PATH"
    exit 1
fi
chmod +x "$LAUNCHER_PATH"

# --- Detect platform ---
install_systemd() {
    echo ":: Installing systemd service..."

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    sudo tee "$service_file" > /dev/null <<UNIT
[Unit]
Description=${SERVICE_NAME} daemon
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
WorkingDirectory=$PROJECT_ROOT
ExecStart=$LAUNCHER_PATH
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

# Hardening
NoNewPrivileges=false
ProtectSystem=false

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"

    echo ""
    echo ":: $SERVICE_NAME installed and running as systemd service."
    echo "   Status:  sudo systemctl status $SERVICE_NAME"
    echo "   Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo "   Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "   Restart: sudo systemctl restart $SERVICE_NAME"
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
        <string>$LAUNCHER_PATH</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
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
    echo ":: $SERVICE_NAME installed and running as launchd service."
    echo "   Logs:    tail -f $log_dir/stdout.log"
    echo "   Stop:    launchctl unload $plist_file"
    echo "   Restart: launchctl unload $plist_file && launchctl load $plist_file"
}

# --- Main ---
echo ":: Installer ($SERVICE_NAME)"
echo "   Project dir: $PROJECT_ROOT"
echo ""

OS="$(uname -s)"
case "$OS" in
    Linux)
        if command -v systemctl &>/dev/null; then
            install_systemd
        else
            echo "Error: systemd not found. Please run launcher.sh manually or set up your init system."
            exit 1
        fi
        ;;
    Darwin)
        install_launchd
        ;;
    *)
        echo "Unsupported platform: $OS"
        echo "Please run ./launcher.sh manually or configure your system's service manager."
        exit 1
        ;;
esac
