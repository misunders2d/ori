#!/bin/bash

BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi
# 🧬 $BOT_NAME: Host-side Supervisor Loop (Signal-Based) - v2.1
# Optimized for NTFS mounts and Docker Hub Rate Limits.

IMAGE_NAME="ori-agent-image"

# First-time interactive setup wizard
if [ ! -f "data/.env" ] || ! grep -q "GOOGLE_API_KEY=" "data/.env"; then
  echo "🧬 [$BOT_NAME] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
fi

while true; do
  echo "🧬 [$BOT_NAME] Starting daemon..."
  
  # Clean up dangling images from previous evolutionary builds to prevent disk bloat
  docker image prune -f --filter "label=project=ori" 2>/dev/null || true
  
  # Optimization: Only --build if the image is missing or an update was requested.
  # This avoids hitting Docker Hub Rate Limits on every single crash/restart.
  if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    echo "🧬 [$BOT_NAME] Image missing. Building..."
    docker compose up --build
  else
    docker compose up
  fi
  
  EXIT_CODE=$?

  if [ $EXIT_CODE -eq 100 ]; then
    echo "🧬 [$BOT_NAME] Update Requested (Signal 100). Pulling and Rebuilding..."
    git pull
    docker compose up --build
  elif [ $EXIT_CODE -eq 101 ]; then
    echo "🧬 [$BOT_NAME] Rollback Requested (Signal 101). Reverting..."
    git checkout HEAD~1
    docker compose up --build
  elif [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
    echo "🧬 [$BOT_NAME] Clean shutdown. Goodbye."
    break
  else
    echo "🧬 [$BOT_NAME] Daemon crashed with code $EXIT_CODE. Cool-down (30s) to avoid rate limits..."
    sleep 30
  fi
done
