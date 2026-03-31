#!/bin/bash

BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '\"\'\r' | xargs 2>/dev/null)
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi
# 🧬 $BOT_NAME: Manual Rollback Override (v3.0)
echo "🧬 [$BOT_NAME] Rolling back to previous version..."
git checkout HEAD~1
docker compose up -d --build
echo "🧬 [$BOT_NAME] Done."
