#!/bin/bash

BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  # Extract BOT_NAME from .env if present - use || true to prevent set -e exit if grep fails
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi
# 🧬 $BOT_NAME: Manual Rollback Override (v3.0)
echo "🧬 [$BOT_NAME] Rolling back to previous version..."
# Fix root-owned .git objects left by container commits
if [ -d ".git" ]; then
  sudo chown -R "$(id -u):$(id -g)" .git 2>/dev/null || true
fi
git reset --hard HEAD~1
git clean -fd --exclude=data --exclude=.env
docker compose up -d --build
echo "🧬 [$BOT_NAME] Done."
