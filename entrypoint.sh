#!/bin/bash
set -e

BOT_NAME="Ori"
if [ -f "/code/data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' /code/data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | xargs 2>/dev/null)
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

echo "🧬 [$BOT_NAME Setup] Aligning container permissions with host..."

TARGET_UID=$(stat -c "%u" /code/.git 2>/dev/null || echo "1000")
TARGET_GID=$(stat -c "%g" /code/.git 2>/dev/null || echo "1000")

if [ "$TARGET_UID" = "0" ]; then TARGET_UID=1000; fi
if [ "$TARGET_GID" = "0" ]; then TARGET_GID=1000; fi

if [ "$TARGET_GID" != "$(id -g agentuser)" ]; then
    groupmod -o -g "$TARGET_GID" agentgroup || true
fi
if [ "$TARGET_UID" != "$(id -u agentuser)" ]; then
    usermod -o -u "$TARGET_UID" agentuser || true
fi

chown -R agentuser:agentgroup /code

gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

# --- AUTO-ROLLBACK WATCHDOG ---
CRASH_FILE="/code/data/.crash_count"
MAX_CRASHES=3

if [ -f "$CRASH_FILE" ]; then
    CRASHES=$(cat "$CRASH_FILE")
else
    CRASHES=0
fi

if [ "$CRASHES" -ge "$MAX_CRASHES" ]; then
    echo "🚨 [$BOT_NAME Watchdog] Detected $CRASHES consecutive crashes! Initiating auto-rollback..."
    gosu agentuser git clean -fd || true
    gosu agentuser git reset --hard HEAD~1 || true
    echo "0" > "$CRASH_FILE"
    echo "🧬 [$BOT_NAME Watchdog] Rollback complete. Proceeding with safe boot."
else
    NEW_CRASHES=$((CRASHES + 1))
    echo "$NEW_CRASHES" > "$CRASH_FILE"
fi

echo "🧬 [$BOT_NAME Setup] Dropping privileges and starting daemon..."

gosu agentuser "$@" &
DAEMON_PID=$!

(
    sleep 30
    if kill -0 $DAEMON_PID 2>/dev/null; then
        echo "🧬 [$BOT_NAME Watchdog] Boot stable for 30s. Resetting crash counter."
        echo "0" > "$CRASH_FILE"
    fi
) &

set +e
wait $DAEMON_PID
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" = "0" ] || [ "$EXIT_CODE" = "100" ] || [ "$EXIT_CODE" = "101" ]; then
    echo "0" > "$CRASH_FILE"
fi

exit $EXIT_CODE
