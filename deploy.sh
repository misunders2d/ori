#!/usr/bin/env bash
# Quick manual rebuild + restart (does not enter the regeneration loop).
# For full supervised operation, use ./launcher.sh or ./install.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BOT_NAME="Ori"
if [ -f "data/.env" ]; then
    ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d "\"'\\r" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' 2>/dev/null || true)
    [ -n "$ENV_BOT_NAME" ] && BOT_NAME="$ENV_BOT_NAME"
fi

echo ":: [$BOT_NAME] Manual deploy: pull, rebuild, restart..."
git pull || true
docker compose up -d --build
docker image prune -f --filter "label=project=ori" 2>/dev/null || true
echo ":: [$BOT_NAME] Done."
