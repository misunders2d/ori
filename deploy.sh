#!/bin/bash

BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | xargs 2>/dev/null)
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi
# 🧬 $BOT_NAME: Manual Update (NTFS-friendly)

# First-time interactive setup wizard
if [ ! -f "data/.env" ] || ! grep -q "GOOGLE_API_KEY=" "data/.env"; then
  echo "🧬 [$BOT_NAME] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it ori-agent uv run python interfaces/setup_wizard.py
fi

echo "🧬 [$BOT_NAME] Forcing manual update..."
git pull
docker compose up -d --build
echo "🧬 [$BOT_NAME] Sweeping old DNA..."
docker image prune -f --filter "label=project=ori" 2>/dev/null || true
echo "🧬 [$BOT_NAME] Done."
