#!/usr/bin/env bash
# Ori — Smart start script. Does the right thing automatically:
#   1. If systemd is available and service isn't installed → installs it
#   2. If systemd service exists → starts/restarts it
#   3. Otherwise → runs launcher.sh in background with logging
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICE_NAME="ori-agent"

if command -v systemctl &>/dev/null; then
    # systemd available
    if systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null && \
       systemctl cat "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        # Service already installed — restart it
        echo ":: Restarting Ori via systemd..."
        sudo systemctl restart "$SERVICE_NAME"
        echo ":: Ori is running."
        echo "   Logs:  sudo journalctl -u $SERVICE_NAME -f"
        echo "   Stop:  sudo systemctl stop $SERVICE_NAME"
    else
        # systemd available but service not installed — install it
        echo ":: First run detected. Installing Ori as a system service..."
        ./install.sh
    fi
else
    # No systemd — run launcher in background
    echo ":: Starting Ori in background..."
    mkdir -p data
    nohup ./launcher.sh >> data/launcher.log 2>&1 &
    echo ":: Ori is running (PID: $!)."
    echo "   Logs:  tail -f data/launcher.log"
    echo "   Stop:  kill $!"
fi
