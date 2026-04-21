#!/usr/bin/env bash
# Ori — Stop the agent.
#
# Does three things, in order:
#   1. Stops the user-level systemd / launchd unit (if registered).
#   2. Sweeps any orphan `ori-supervisor.py` processes belonging to THIS project
#      (matched on the absolute path so a multi-bot host stays safe).
#   3. Stops the Cloudflare tunnel container if present.
#
# The orphan sweep exists because a crashed service, an aborted start, or a
# stale manual run can leave a supervisor still holding the Telegram polling
# slot — causing `409 Conflict: terminated by other getUpdates request` when
# a new instance boots.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Derive service name
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ] && command -v python3 &>/dev/null; then
    _env_name=$(python3 -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# --- 1. Stop the service ---
OS="$(uname -s)"
case "$OS" in
    Linux)
        if systemctl --user is-active "$SERVICE_NAME" &>/dev/null; then
            systemctl --user stop "$SERVICE_NAME"
            echo ":: $SERVICE_NAME service stopped."
        else
            echo ":: $SERVICE_NAME service not running."
        fi
        ;;
    Darwin)
        plist="$HOME/Library/LaunchAgents/com.${SERVICE_NAME}.plist"
        if [ -f "$plist" ]; then
            launchctl unload "$plist" 2>/dev/null || true
            echo ":: $SERVICE_NAME service stopped."
        else
            echo ":: $SERVICE_NAME service not running."
        fi
        ;;
esac

# --- 2. Sweep orphan supervisor processes for THIS project ---
# Matching on the absolute supervisor path keeps this safe on hosts where
# multiple bots share the same template — we only touch processes whose
# command line references the current checkout.
_sweep_pattern="$PROJECT_ROOT/deploy/ori-supervisor.py"
_orphans=$(pgrep -f "$_sweep_pattern" 2>/dev/null || true)

if [ -n "$_orphans" ]; then
    # Strip own PID out of the match (defensive — pgrep -f can match its own cmdline on some platforms).
    _orphans=$(echo "$_orphans" | grep -v "^$$\$" || true)
fi

if [ -n "$_orphans" ]; then
    echo ":: Orphan supervisor(s) still alive — sending SIGTERM: $(echo $_orphans | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill $_orphans 2>/dev/null || true

    # Give them up to 3s to wind down gracefully before escalating.
    for _ in 1 2 3; do
        sleep 1
        _orphans=$(pgrep -f "$_sweep_pattern" 2>/dev/null | grep -v "^$$\$" || true)
        [ -z "$_orphans" ] && break
    done

    _orphans=$(pgrep -f "$_sweep_pattern" 2>/dev/null | grep -v "^$$\$" || true)
    if [ -n "$_orphans" ]; then
        echo ":: SIGTERM timed out after 3s; escalating to SIGKILL: $(echo $_orphans | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill -9 $_orphans 2>/dev/null || true
    fi
    echo ":: Orphan sweep done."
fi

# --- 3. Stop Cloudflare tunnel ---
if command -v docker &>/dev/null; then
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true
fi

echo ":: Stop complete."
