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
    echo "  [.] Waiting for daemon stability check (15s)..."
    sleep 15
    if [ "$(docker inspect -f '{{.State.Running}}' ori-agent-daemon 2>/dev/null)" = "true" ]; then
        return 0
    fi
    return 1
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

        # Pop previous stable commit off local head securely
        PREVIOUS_COMMIT=$(git log -2 --format="%H" | tail -n 1)
        
        echo "  [!] Rolling back single evolution node to $PREVIOUS_COMMIT..."
        git reset --hard "$PREVIOUS_COMMIT"
        
        # Clear out remote tracking temporarily so next update works properly
        git fetch origin master 2>&1 || true

        chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh"
        
        echo "  [+] Force compiling previous stable..."
        docker compose build 2>&1
        docker compose up -d 2>&1

        if wait_for_healthy; then
            echo "$(date): Rollback Sequence completed."
            send_notification "$TRIGGER_CONTENT" "✅ Rollback Sequence Complete. Daemon stabilized onto previous Git signature."
            echo "  [+] Cleaning up dangling Docker build caches..."
            docker image prune -f --filter "dangling=true" > /dev/null 2>&1
        else
            echo "$(date): Rollback structure failed. Re-initiating container cycle..."
            send_notification "$TRIGGER_CONTENT" "🚨 FATAL: The previous stable code also crashed on boot. You must manually unbrick via SSH!"
        fi
        
        echo "=========================================="
    fi
    sleep 5
done
