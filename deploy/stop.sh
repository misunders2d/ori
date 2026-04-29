#!/usr/bin/env bash
# Ori — Stop the agent AND disable auto-start on reboot.
#
# Symmetric with start.sh:
#   start.sh = enable + start
#   stop.sh  = stop + disable
#
# Does, in order:
#   1. Stops AND disables the user-level systemd / launchd unit (if registered).
#      "Disable" means the unit will NOT auto-start on next reboot/login.
#   2. Sweeps any orphan `ori-supervisor.py` processes belonging to THIS project
#      (matched on the absolute path so a multi-bot host stays safe).
#   3. Stops the Cloudflare tunnel container if present.
#
# The orphan sweep exists because a crashed service, an aborted start, or a
# stale manual run can leave a supervisor still holding the Telegram polling
# slot — causing `409 Conflict: terminated by other getUpdates request` when
# a new instance boots.
#
# Project files, vault, code, git history are NEVER touched here.
# For the destructive "delete everything" path, use deploy/uninstall.sh.
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

# Pick up the originally-installed name in case BOT_NAME changed since install.
ORIGINAL_SERVICE_NAME=""
if [ -f "$PROJECT_ROOT/data/.service_name" ]; then
    ORIGINAL_SERVICE_NAME=$(cat "$PROJECT_ROOT/data/.service_name" 2>/dev/null || true)
fi

# --- 1. Stop AND disable the service ---
OS="$(uname -s)"
case "$OS" in
    Linux)
        _stop_and_disable() {
            local svc="$1"
            [ -z "$svc" ] && return
            if ! systemctl --user cat "$svc" &>/dev/null; then
                return  # unit doesn't exist on this machine
            fi
            if systemctl --user is-active "$svc" &>/dev/null; then
                systemctl --user stop "$svc"
                echo ":: $svc stopped."
            else
                echo ":: $svc was already stopped."
            fi
            if systemctl --user is-enabled "$svc" &>/dev/null; then
                systemctl --user disable "$svc"
                echo ":: $svc disabled — will NOT auto-start on reboot."
            fi
        }
        _stop_and_disable "$SERVICE_NAME"
        if [ -n "$ORIGINAL_SERVICE_NAME" ] && [ "$ORIGINAL_SERVICE_NAME" != "$SERVICE_NAME" ]; then
            _stop_and_disable "$ORIGINAL_SERVICE_NAME"
        fi
        ;;
    Darwin)
        _unload_persistently() {
            local svc="$1"
            [ -z "$svc" ] && return
            local plist="$HOME/Library/LaunchAgents/com.${svc}.plist"
            [ ! -f "$plist" ] && return
            # `unload -w` adds a persistent Disabled flag in the override DB
            # so the agent does NOT load on next login. The plist itself stays.
            launchctl unload -w "$plist" 2>/dev/null || launchctl unload "$plist" 2>/dev/null || true
            echo ":: $svc unloaded and disabled."
        }
        _unload_persistently "$SERVICE_NAME"
        if [ -n "$ORIGINAL_SERVICE_NAME" ] && [ "$ORIGINAL_SERVICE_NAME" != "$SERVICE_NAME" ]; then
            _unload_persistently "$ORIGINAL_SERVICE_NAME"
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
