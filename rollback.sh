#!/bin/bash
# Independent Rollback watcher for the Ori Daemon
# Triggered strictly when the agent fails or human orders a blind revert.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIGGER_FILE="$SCRIPT_DIR/data/.rollback_trigger"
ENV_FILE="$SCRIPT_DIR/data/.env"

read_env_value() {
    local key="$1"
    if [ ! -f "$ENV_FILE" ]; then return 1; fi
    local val=$(grep "^${key}=" "$ENV_FILE" | head -1 | sed "s/^${key}=//")
    val=$(echo "$val" | sed "s/^['\"]//;s/['\"]$//")
    echo "$val"
}

send_notification() {
    local trigger_content="$1"
    local message="$2"
    local notify_type=$(echo "$trigger_content" | python3 -c "import sys,json; print(json.load(sys.stdin).get('notify',{}).get('type',''))" 2>/dev/null)
    
    if [ "$notify_type" = "telegram" ]; then
        local chat_id=$(echo "$trigger_content" | python3 -c "import sys,json; print(json.load(sys.stdin)['notify']['chat_id'])" 2>/dev/null)
        local bot_token=$(read_env_value TELEGRAM_BOT_TOKEN)
        
        if [ -n "$bot_token" ] && [ -n "$chat_id" ]; then
            curl -s -X POST "https://api.telegram.org/bot${bot_token}/sendMessage" \
                -H "Content-Type: application/json" \
                -d "{\"chat_id\": ${chat_id}, \"text\": \"${message}\"}" > /dev/null 2>&1
        fi
    fi
}

wait_for_healthy() {
    echo "
[.] Waiting for daemon stability check (15s)..."
    sleep 15
    if [ "$(docker inspect -f '{{.State.Running}}' ori-agent-daemon 2>/dev/null)" = "true" ]; then
        return 0
    fi
    return 1
}

cleanup_docker() {
    echo "
[+] Decluttering Docker artifacts..."
    # Remove dangling images (the <none> ones created during rebuilds)
    docker image prune -f --filter "dangling=true" > /dev/null 2>&1
    # Remove stopped containers to prevent accumulation
    docker container prune -f > /dev/null 2>&1
}

echo "$(date): Ori Daemon Rollback Switch Online. Monitoring For Abort Signals..."

while true; do
    if [ -f "$TRIGGER_FILE" ]; then
        echo ""
        echo "=========================================="
        echo "$(date): ROLLBACK SIGNAL DETECTED. SEIZING HOST CONTROL."
        echo "=========================================="
        
        TRIGGER_CONTENT=$(cat "$TRIGGER_FILE")
        rm -f "$TRIGGER_FILE"
        
        sleep 10
        cd "$SCRIPT_DIR"
        
        # --- Step 1: Align to remote truth ---
        echo "
[+] Fetching authoritative remote state..."
        if ! git fetch origin master 2>&1; then
            echo "
[-] FATAL: Cannot reach origin. Rollback impossible without remote."
            send_notification "$TRIGGER_CONTENT" "⚠️ Rollback Failed: Cannot reach git remote. Check network/SSH keys."
            continue
        fi
        git reset --hard origin/master 2>&1 || true
        git clean -fd 2>&1 || true
        
        # --- Step 2: Revert HEAD on remote ---
        CURRENT_HEAD=$(git rev-parse --short HEAD 2>/dev/null)
        echo "
[!] Reverting commit $CURRENT_HEAD on remote..."
        
        if ! git revert HEAD --no-edit 2>&1; then
            echo "
[-] FATAL: git revert failed (merge commit or conflict)."
            send_notification "$TRIGGER_CONTENT" "⚠️ Rollback Failed: Could not cleanly revert HEAD ($CURRENT_HEAD). Manual revert required."
            git revert --abort 2>/dev/null || true
            continue
        fi
        
        if ! git push origin master 2>&1; then
            echo "
[-] FATAL: Push to remote failed."
            send_notification "$TRIGGER_CONTENT" "⚠️ Rollback Failed: Revert created locally but push to origin rejected. Check remote permissions."
            continue
        fi
        
        echo "
[+] Revert pushed to origin/master."
        
        # --- Step 3: Rebuild and restart ---
        chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh"
        
        echo "
[+] Rebuilding daemon from reverted code..."
        docker compose build 2>&1
        docker compose up -d 2>&1
        
        if wait_for_healthy; then
            echo "$(date): Rollback Sequence completed."
            send_notification "$TRIGGER_CONTENT" "✅ Rollback complete. Reverted $CURRENT_HEAD on origin/master. Daemon rebuilt and stable."
            cleanup_docker
        else
            echo "$(date): Rollback structure failed. Reverted code also crashes."
            send_notification "$TRIGGER_CONTENT" "🚨 FATAL: Reverted code ($CURRENT_HEAD~1) also crashed on boot. Manual SSH intervention required."
        fi
        echo "=========================================="
    fi
    sleep 5
done
