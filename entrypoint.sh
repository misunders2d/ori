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

# Only chown if we actually changed something to save boot time
echo "🧬 [$BOT_NAME Setup] Ensuring file ownership..."
chown -R agentuser:agentgroup /code /home/agentuser

# Targeted permissions fix for data directory to resolve SQLite lockouts (using 777/666 for Docker sync reliability)
echo "🧬 [$BOT_NAME Setup] Hardening data permissions..."
chmod 777 /code/data || true
find /code/data -maxdepth 2 -type f -exec chmod 666 {} + || true

# Enable SQLite WAL mode for better concurrency and fewer read-only locks
if command -v sqlite3 >/dev/null 2>&1; then
    echo "🧬 [$BOT_NAME Setup] Optimizing database concurrency (WAL mode)..."
    for db in /code/data/*.db; do
        if [ -f "$db" ]; then
            sqlite3 "$db" "PRAGMA journal_mode=WAL;" || true
        fi
    done
fi

# Git configuration for the agent user
gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

# --- 2. DATABASE HYGIENE (Reboot Protection) ---
# Remove stale SQLite lock files that can cause 'Read-only database' errors after a crash/reboot
echo "🧬 [$BOT_NAME Setup] Clearing stale database locks..."
find /code/data -maxdepth 2 -name "*.db-journal" -delete || true
find /code/data -maxdepth 2 -name "*.db-wal" -delete || true
find /code/data -maxdepth 2 -name "*.db-shm" -delete || true

# --- 3. AUTO-ROLLBACK WATCHDOG ---
CRASH_FILE="/code/data/.crash_count"
MAX_CRASHES=3

if [ -f "$CRASH_FILE" ]; then
    CRASH_FILE_CONTENT=$(cat "$CRASH_FILE")
    # Basic numeric validation
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
    # Preserve data directory and env files explicitly
    gosu agentuser git clean -fd --exclude=data --exclude=.env || true
    gosu agentuser git reset --hard HEAD~1 || true
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
