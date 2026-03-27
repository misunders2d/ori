#!/bin/bash
# Ori Daemon Setup & Launch script
# Usage:
#   chmod +x start.sh
#   ./start.sh [--no-sync]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "    Ori Daemon — Setup & Launch Core"
echo "=========================================="
echo ""

# --- Check Prerequisites ---
for cmd in docker git python3; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "ERROR: '$cmd' is not installed. Please install it on the host."
        exit 1
    fi
done

if ! docker compose version &> /dev/null; then
    echo "ERROR: 'docker compose' (v2) not available."
    exit 1
fi

if ! docker info &> /dev/null; then
    echo "ERROR: Docker daemon is not running, or you lack permissions."
    exit 1
fi

# --- Sync remote codebase ---
if [[ "$1" == "--no-sync" ]]; then
    echo "  [.] Skipping remote git sync..."
else
    echo "  [+] Fetching and syncing origin master..."
    git fetch origin master 2>&1
    git reset --hard origin/master 2>&1
fi
chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh"

# --- Prepare data buffers ---
mkdir -p "$SCRIPT_DIR/data"
chmod -R 777 "$SCRIPT_DIR/data" # Ensure non-root container user can write state

# --- Migrate legacy single-database if needed ---
if [ -f "$SCRIPT_DIR/data/ori.db" ] && [ ! -f "$SCRIPT_DIR/data/ori-sessions.db" ]; then
    echo "  [+] Migrating ori.db into separate session/scheduler databases..."
    python3 "$SCRIPT_DIR/scripts/migrate_split_db.py"
fi

# --- Launch Container Stack ---
echo "  [+] Tearing down old instances and rebuilding..."
docker compose down
docker compose up --build -d

# --- Restart Async Host Supervisors ---
DEPLOY_PID_FILE="$SCRIPT_DIR/data/.deploy_watcher.pid"
ROLLBACK_PID_FILE="$SCRIPT_DIR/data/.rollback_watcher.pid"

for PID_FILE in "$DEPLOY_PID_FILE" "$ROLLBACK_PID_FILE"; do
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if kill -0 "$OLD_PID" 2>/dev/null; then
            kill "$OLD_PID" 2>/dev/null || true
            sleep 1
        fi
        rm -f "$PID_FILE"
    fi
done

# Start cleanly
nohup "$SCRIPT_DIR/deploy.sh" >> "$SCRIPT_DIR/data/deploy.log" 2>&1 &
echo $! > "$DEPLOY_PID_FILE"

nohup "$SCRIPT_DIR/rollback.sh" >> "$SCRIPT_DIR/data/rollback.log" 2>&1 &
echo $! > "$ROLLBACK_PID_FILE"

echo ""
echo "=========================================="
echo "  Ori is Active & Isolated"
echo "=========================================="
echo "  Logs:        docker logs -f ori-agent-daemon"
echo "  Deploy:      tail -f data/deploy.log"
echo "  Rollback:    tail -f data/rollback.log"
echo "  Stop Core:   docker compose down"
echo "=========================================="
echo ""
