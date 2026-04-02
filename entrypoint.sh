#!/bin/bash
set -e

# --- 1. SYSTEM IDENTITY & PERMISSIONS ---
BOT_NAME="Ori"
if [ -f "/code/data/.env" ]; then
  # Extract BOT_NAME from .env if present
  ENV_BOT_NAME=$(grep -v '^#' /code/data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

echo "🧬 [$BOT_NAME Setup] Aligning container permissions with host..."

# Improved UID/GID detection: Check ENV (best), then .git, then data/, then current directory
if [ ! -z "$AGENT_UID" ] && [ ! -z "$AGENT_GID" ]; then
    TARGET_UID="$AGENT_UID"
    TARGET_GID="$AGENT_GID"
elif [ -d "/code/.git" ]; then
    TARGET_UID=$(stat -c "%u" /code/.git)
    TARGET_GID=$(stat -c "%g" /code/.git)
elif [ -d "/code/data" ]; then
    TARGET_UID=$(stat -c "%u" /code/data)
    TARGET_GID=$(stat -c "%g" /code/data)
else
    TARGET_UID=$(stat -c "%u" /code)
    TARGET_GID=$(stat -c "%g" /code)
fi

# Fallback for root-cloned repos or misconfigured ENV to ensure we don't run as root internally
if [ "$TARGET_UID" = "0" ]; then TARGET_UID=1000; fi
if [ "$TARGET_GID" = "0" ]; then TARGET_GID=1000; fi

# Remap internal agentuser to match host UID/GID for seamless bind-mount access
if [ "$TARGET_GID" != "$(id -g agentuser)" ]; then
    groupmod -o -g "$TARGET_GID" agentgroup || true
fi
if [ "$TARGET_UID" != "$(id -u agentuser)" ]; then
    usermod -o -u "$TARGET_UID" agentuser || true
fi

# --- 2. DATABASE HYGIENE (Reboot Protection) ---
# Checkpoint WAL databases and remove stale locks safely.
# IMPORTANT: Never delete .db-wal files blindly — un-checkpointed WAL data
# would be lost, leaving the main .db corrupt/read-only.
echo "🧬 [$BOT_NAME Setup] Checkpointing databases..."
find /code/data -maxdepth 2 -name "*.db-journal" -delete 2>/dev/null || true
for db in /code/data/*.db /code/data/**/*.db; do
    [ -f "$db" ] || continue
    # Checkpoint flushes WAL into the main DB, then TRUNCATE clears the WAL file
    sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
done

# Ensure file ownership (after cleanup so no root-owned lock files linger)
echo "🧬 [$BOT_NAME Setup] Ensuring file ownership..."
chown -R agentuser:agentgroup /code /home/agentuser

# WAL mode is set at application startup (run_bot.py) as agentuser — not here as root,
# which would create root-owned .db-wal/.db-shm files and cause 'readonly database' errors.

# Git configuration for the agent user
gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

# --- 3. AUTO-ROLLBACK WATCHDOG ---
CRASH_FILE="/code/data/.crash_count"
MAX_CRASHES=3

if [ -f "$CRASH_FILE" ]; then
    CRASH_FILE_CONTENT=$(cat "$CRASH_FILE")
    if [[ "$CRASH_FILE_CONTENT" =~ ^[0-9]+$ ]]; then
        CRASHES=$CRASH_FILE_CONTENT
    else
        CRASHES=0
    fi
else
    CRASHES=0
fi

if [ "$CRASHES" -ge "$MAX_CRASHES" ]; then
    echo "🚨 [$BOT_NAME Watchdog] Detected $CRASHES consecutive crashes! Initiating auto-rollback..."
    # Backup .env BEFORE any git operations — this is the most critical file to preserve
    if [ -f "/code/data/.env" ]; then
        cp -a /code/data/.env /code/data/.env.backup || true
    fi
    # Preserve data directory and env files explicitly
    gosu agentuser git clean -fd --exclude=data --exclude='data/*' --exclude=.env || true
    gosu agentuser git reset --hard HEAD~1 || true
    # Restore .env if it was lost during rollback
    if [ ! -f "/code/data/.env" ] && [ -f "/code/data/.env.backup" ]; then
        echo "🧬 [$BOT_NAME Watchdog] Restoring .env from backup..."
        mv /code/data/.env.backup /code/data/.env
    fi
    echo "🧬 [$BOT_NAME Watchdog] Rollback complete. Proceeding with safe boot."
    CRASHES=0
fi

echo "$((CRASHES + 1))" > "$CRASH_FILE"
chown agentuser:agentgroup "$CRASH_FILE"

# --- 4. DAEMON STARTUP ---
echo "🧬 [$BOT_NAME Setup] Dropping privileges and starting daemon..."

# Background stability check: if the main daemon survives 30s, reset the crash counter
(
    sleep 30
    if kill -0 $$ 2>/dev/null; then
        echo "🧬 [$BOT_NAME Watchdog] Boot stable for 30s. Resetting crash counter."
        gosu agentuser sh -c "echo 0 > $CRASH_FILE"
    fi
) &

# Run the command passed to the entrypoint (usually the bot startup) IN THE FOREGROUND
# This is required so interactive standard input (stdin) works for the CLI fallback.
export CRASH_FILE
exec gosu agentuser "$@"
