#!/bin/bash
set -e

BOT_NAME="Ori"
if [ -f "/code/data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' /code/data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | xargs 2>/dev/null)
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

echo "🧬 [$BOT_NAME Setup] Aligning container permissions with host..."

# Read host UID/GID from the mounted .git directory (which the host user owns)
TARGET_UID=$(stat -c "%u" /code/.git 2>/dev/null || echo "1000")
TARGET_GID=$(stat -c "%g" /code/.git 2>/dev/null || echo "1000")

if [ "$TARGET_UID" = "0" ]; then TARGET_UID=1000; fi
if [ "$TARGET_GID" = "0" ]; then TARGET_GID=1000; fi

echo "🧬 [$BOT_NAME Setup] Host UID: $TARGET_UID, GID: $TARGET_GID"

# Modify the internal agentuser to match the host UID/GID
if [ "$TARGET_GID" != "$(id -g agentuser)" ]; then
    groupmod -o -g "$TARGET_GID" agentgroup || true
fi
if [ "$TARGET_UID" != "$(id -u agentuser)" ]; then
    usermod -o -u "$TARGET_UID" agentuser || true
fi

# Fix ownership of all files so the newly mapped user can read/write them
echo "🧬 [$BOT_NAME Setup] Securing internal directories and fixing host permissions..."
chown -R agentuser:agentgroup /code

# Configure git for the unprivileged user
gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "$BOT_NAME Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@$BOT_NAME-agent.local" || true

echo "🧬 [$BOT_NAME Setup] Dropping privileges and starting daemon..."
# Drop root privileges and run the main command
exec gosu agentuser "$@"
