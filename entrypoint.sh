#!/bin/bash
set -e

# ============================================================================
# Ori Container Entrypoint — Minimal container init
# ============================================================================
# Handles ONLY: permission alignment, database hygiene, git config, exec.
# All recovery logic (crash counting, rollback) lives in launcher.sh.
# ============================================================================

# --- 1. SYSTEM IDENTITY & PERMISSIONS ---
BOT_NAME="Ori"
if [ -f "/code/data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' /code/data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

echo ":: [$BOT_NAME Container] Aligning permissions..."

# Detect target UID/GID from env (set by launcher) or infer from mounted dirs
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

# Never run as root inside the container
if [ "$TARGET_UID" = "0" ]; then TARGET_UID=1000; fi
if [ "$TARGET_GID" = "0" ]; then TARGET_GID=1000; fi

# Remap agentuser to match host UID/GID
if [ "$TARGET_GID" != "$(id -g agentuser)" ]; then
    groupmod -o -g "$TARGET_GID" agentgroup || true
fi
if [ "$TARGET_UID" != "$(id -u agentuser)" ]; then
    usermod -o -u "$TARGET_UID" agentuser || true
fi

# --- 2. DATABASE HYGIENE ---
echo ":: [$BOT_NAME Container] Checkpointing databases..."
find /code/data -maxdepth 2 -name "*.db-journal" -delete 2>/dev/null || true
for db in /code/data/*.db /code/data/**/*.db; do
    [ -f "$db" ] || continue
    sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
done

# Fix ownership
chown -R agentuser:agentgroup /code /home/agentuser

# --- 3. GIT CONFIG ---
gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME (Agent)" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

# --- 4. EXEC ---
echo ":: [$BOT_NAME Container] Starting daemon..."
exec gosu agentuser "$@"
