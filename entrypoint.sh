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

gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

# Fix ownership of ALL code files before any git operations.
# Root-owned files (from prior boots or docker layer caching) block git checkout and uv.
echo "🧬 [$BOT_NAME Setup] Fixing file ownership..."
chown -R agentuser:agentgroup /code

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
    # Preserve data directory (not tracked by git, contains .env and databases)
    gosu agentuser git clean -fd --exclude=data/ || true
    gosu agentuser git reset --hard HEAD~1 || true
    echo "🧬 [$BOT_NAME Watchdog] Rollback complete. Proceeding with safe boot."
    CRASHES=0
fi

echo "$((CRASHES + 1))" > "$CRASH_FILE"
chown agentuser:agentgroup "$CRASH_FILE"

echo "🧬 [$BOT_NAME Setup] Dropping privileges and starting daemon..."

gosu agentuser "$@" &
DAEMON_PID=$!

(
    sleep 30
    if kill -0 $DAEMON_PID 2>/dev/null; then
        echo "🧬 [$BOT_NAME Watchdog] Boot stable for 30s. Resetting crash counter."
        gosu agentuser sh -c "echo 0 > $CRASH_FILE"
    fi
) &

set +e
wait $DAEMON_PID
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" = "0" ] || [ "$EXIT_CODE" = "100" ] || [ "$EXIT_CODE" = "101" ]; then
    gosu agentuser sh -c "echo 0 > $CRASH_FILE"
fi

exit $EXIT_CODE
