#!/usr/bin/env bash
# ============================================================================
# Ori — Stop the agent AND disable auto-start on reboot.
#
# The middle ground between stop.sh (transient) and uninstall.sh (deletes
# everything). Project directory, vault, code, git history all stay intact.
#
# What this does:
#   - Stops the running service (idempotent, same as stop.sh)
#   - Disables the systemd unit / unloads the launchd plist persistently
#     so it does NOT auto-start on next reboot
#   - Stops the Cloudflare tunnel container if running
#
# What this does NOT do:
#   - Remove the systemd unit file / launchd plist  (so re-enabling is fast)
#   - Remove user-level lingering (other user services may need it)
#   - Touch any data, vault, code, or git state
#
# To bring her back later:
#   Linux:  systemctl --user enable <service>-agent && deploy/start.sh
#   macOS:  launchctl load ~/Library/LaunchAgents/com.<service>-agent.plist
#           or just deploy/start.sh
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ---- Derive service name (same as stop.sh / uninstall.sh) ----
_bot_name="ori"
VAULT_FILE="$PROJECT_ROOT/data/vault/credentials.json"
if [ -f "$VAULT_FILE" ] && command -v python3 &>/dev/null; then
    _env_name=$(python3 -c "import json; print(json.load(open('$VAULT_FILE')).get('BOT_NAME',''))" 2>/dev/null || true)
    [ -n "$_env_name" ] && _bot_name="$_env_name"
fi
SERVICE_NAME="$(echo "$_bot_name" | tr '[:upper:]' '[:lower:]' | tr ' _' '-' | sed 's/[^a-z0-9-]//g')-agent"

# Also pick up the originally-installed name in case BOT_NAME changed since.
ORIGINAL_SERVICE_NAME=""
if [ -f "$PROJECT_ROOT/data/.service_name" ]; then
    ORIGINAL_SERVICE_NAME=$(cat "$PROJECT_ROOT/data/.service_name" 2>/dev/null || true)
fi

OS="$(uname -s)"
case "$OS" in
    Linux)
        _disable_one() {
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
            else
                echo ":: $svc was already disabled."
            fi
        }
        _disable_one "$SERVICE_NAME"
        if [ -n "$ORIGINAL_SERVICE_NAME" ] && [ "$ORIGINAL_SERVICE_NAME" != "$SERVICE_NAME" ]; then
            _disable_one "$ORIGINAL_SERVICE_NAME"
        fi
        ;;
    Darwin)
        _disable_plist() {
            local svc="$1"
            [ -z "$svc" ] && return
            local plist="$HOME/Library/LaunchAgents/com.${svc}.plist"
            if [ ! -f "$plist" ]; then
                return
            fi
            # `unload -w` adds a persistent Disabled flag in the override DB
            # so the agent does NOT load on next login. The plist itself stays.
            launchctl unload -w "$plist" 2>/dev/null || launchctl unload "$plist" 2>/dev/null || true
            echo ":: $svc unloaded and marked disabled."
        }
        _disable_plist "$SERVICE_NAME"
        if [ -n "$ORIGINAL_SERVICE_NAME" ] && [ "$ORIGINAL_SERVICE_NAME" != "$SERVICE_NAME" ]; then
            _disable_plist "$ORIGINAL_SERVICE_NAME"
        fi
        ;;
    *)
        echo "  WARNING: unsupported OS ($OS) — skipping service-disable step."
        ;;
esac

# ---- Stop Cloudflare tunnel if Docker is around ----
if command -v docker &>/dev/null; then
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" down 2>/dev/null || true
fi

echo ""
echo "============================================"
echo "  $_bot_name is stopped and disabled."
echo "  Project files at $PROJECT_ROOT are untouched."
echo ""
echo "  To re-enable:"
case "$OS" in
    Linux)  echo "    systemctl --user enable $SERVICE_NAME && deploy/start.sh" ;;
    Darwin) echo "    deploy/start.sh" ;;
esac
echo "============================================"
