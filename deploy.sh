#!/bin/bash
# Auto-deploy watcher for the Ori Daemon
# Triggered strictly when the agent successfully self-evolves and signals via `.update_trigger`

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIGGER_FILE="$SCRIPT_DIR/data/.update_trigger"
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
            echo "  [+] Notification sent to Telegram chat ${chat_id}"
        fi
    fi
}

wait_for_healthy() {
    echo "  [.] Waiting for daemon stability check (15s)..."
    sleep 15
    if [ "$(docker inspect -f '{{.State.Running}}' ori-agent-daemon 2>/dev/null)" = "true" ]; then
        echo "  [+] Daemon container persists and is stable."
        return 0
    fi
    echo "  [-] FATAL: Container crashed on boot!"
    return 1
}

rollback() {
    local previous_commit="$1"
    local trigger_content="$2"

    echo "  [!] Rolling back logic directly to stable signature: $previous_commit..."
    git reset --hard "$previous_commit"
    chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh"
    docker compose build 2>&1
    docker compose up -d 2>&1

    if wait_for_healthy; then
        echo "$(date): Rollback successful."
        send_notification "$trigger_content" "✅ Stability fallback successful. Reverted to previous stable logic cycle."
    else
        echo "$(date): FATAL CASCADING FAILURE."
        send_notification "$trigger_content" "🚨 FATAL: Fallback failed to boot. Core loop broken. Manual SSH intervention required immediately."
    fi
}

echo "$(date): Ori Daemon Watcher Online. Monitoring for Update Signals..."

while true; do
    if [ -f "$TRIGGER_FILE" ]; then
        echo ""
        echo "=========================================="
        echo "$(date): UPDATE SIGNAL DETECTED. SEIZING HOST CONTROL."
        echo "=========================================="

        TRIGGER_CONTENT=$(cat "$TRIGGER_FILE")
        rm -f "$TRIGGER_FILE"

        # Give agent 10s to cleanly exit current API request handling
        sleep 10
        cd "$SCRIPT_DIR"

        PREVIOUS_COMMIT=$(git rev-parse HEAD)
        
        # Pull strict upstream truth and clear out sandbox drift
        echo "  [+] Fetching authoritative remote footprint..."
        git fetch origin master 2>&1
        GIT_OUTPUT=$(git reset --hard origin/master 2>&1)
        GIT_STATUS=$?
        
        chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/deploy.sh" "$SCRIPT_DIR/rollback.sh"

        if [ $GIT_STATUS -ne 0 ]; then
            echo "  [-] Signal failed: Hard remote reset crashed"
            send_notification "$TRIGGER_CONTENT" "⚠️ Update Loop Aborted: Git sequence failed. Check deploy.log."
            continue
        fi

        if [ "$PREVIOUS_COMMIT" = "$(git rev-parse HEAD)" ]; then
            echo "  [.] Zero-delta signal. Agent architecture is unchanged."
            continue
        fi

        echo "  [+] Building fresh daemon container..."
        if ! docker compose build 2>&1; then
            send_notification "$TRIGGER_CONTENT" "⚠️ Compile Failure. Container failed to package. Initiating structural rollback..."
            rollback "$PREVIOUS_COMMIT" "$TRIGGER_CONTENT"
            continue
        fi

        echo "  [+] Orchestrating local cluster swap..."
        if ! docker compose up -d 2>&1; then
            send_notification "$TRIGGER_CONTENT" "⚠️ Upstream Failure. Compose daemon rejected start sequence. Initiating structural rollback..."
            rollback "$PREVIOUS_COMMIT" "$TRIGGER_CONTENT"
            continue
        fi

        if wait_for_healthy; then
            echo "$(date): Evolution sequence closed successfully."
            send_notification "$TRIGGER_CONTENT" "💠 Self-Evolution Successful. Daemon rebuilt and actively polling."
            echo "  [+] Cleaning up dangling Docker build caches..."
            docker image prune -f --filter "dangling=true" > /dev/null 2>&1
        else
            echo "$(date): Core loop rejection on start! Rolling back mutations."
            send_notification "$TRIGGER_CONTENT" "🚨 CRITICAL: The newly compiled code immediately crashed the daemon container upon boot. Triggering structural rollback..."
            rollback "$PREVIOUS_COMMIT" "$TRIGGER_CONTENT"
        fi

        echo "=========================================="
    fi
    sleep 5
done
