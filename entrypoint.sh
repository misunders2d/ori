#!/bin/bash
set -e

echo "🧬 [Ori Setup] Aligning container permissions with host..."

# Read host UID/GID from the mounted .git directory (which the host user owns)
TARGET_UID=$(stat -c "%u" /code/.git 2>/dev/null || echo "1000")
TARGET_GID=$(stat -c "%g" /code/.git 2>/dev/null || echo "1000")

if [ "$TARGET_UID" = "0" ]; then TARGET_UID=1000; fi
if [ "$TARGET_GID" = "0" ]; then TARGET_GID=1000; fi

echo "🧬 [Ori Setup] Host UID: $TARGET_UID, GID: $TARGET_GID"

# Modify the internal agentuser to match the host UID/GID
if [ "$TARGET_GID" != "$(id -g agentuser)" ]; then
    groupmod -o -g "$TARGET_GID" agentgroup || true
fi
if [ "$TARGET_UID" != "$(id -u agentuser)" ]; then
    usermod -o -u "$TARGET_UID" agentuser || true
fi

# Fix ownership of any files previously created as root or UID 999
echo "🧬 [Ori Setup] Securing internal directories and fixing host permissions..."
chown -R agentuser:agentgroup /code/.cache 2>/dev/null || true
chown -R agentuser:agentgroup /code/data 2>/dev/null || true
chown -R agentuser:agentgroup /code/.git 2>/dev/null || true

# Configure git for the unprivileged user
gosu agentuser git config --global --add safe.directory /code || true
gosu agentuser git config --global user.name "Ori Autonomous Daemon" || true
gosu agentuser git config --global user.email "bot@ori-agent.local" || true

echo "🧬 [Ori Setup] Dropping privileges and starting daemon..."
# Drop root privileges and run the main command
exec gosu agentuser "$@"
