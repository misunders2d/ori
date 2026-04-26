#!/usr/bin/env bash
# Ori — Stop the agent AND disable auto-start on reboot.
#
# Symmetric with start.sh:
#   start.sh = enable + start
#   stop.sh  = stop + disable
#
# Project files, vault, code, git history are NEVER touched here.
# For the destructive "delete everything" path, use deploy/uninstall.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Derive service name (matches install.sh / start.sh)
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

# Stop the Cloudflare tunnel container too — scoped to THIS instance only.
# We bring down the project named after this bot AND force-remove the
# container by name as a belt-and-braces (the container name is fixed by
# the BOT_NAME env in docker-compose.yml regardless of compose project).
if command -v docker &>/dev/null; then
    _tunnel_name="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')"
    docker compose -p "$_tunnel_name" -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true
    docker rm -f "${_tunnel_name}-tunnel" 2>/dev/null || true
fi
