#!/bin/bash
# 🧬 Ori: Host-side Supervisor Loop (Signal-Based) - v2.1
# Optimized for NTFS mounts and Docker Hub Rate Limits.

IMAGE_NAME="ori-ori-agent"

while true; do
  echo "🧬 [Ori] Starting daemon..."
  
  # Optimization: Only --build if the image is missing or an update was requested.
  # This avoids hitting Docker Hub Rate Limits on every single crash/restart.
  if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    echo "🧬 [Ori] Image missing. Building..."
    docker compose up --build
  else
    docker compose up
  fi
  
  EXIT_CODE=$?

  if [ $EXIT_CODE -eq 100 ]; then
    echo "🧬 [Ori] Update Requested (Signal 100). Pulling and Rebuilding..."
    git pull
    docker compose up --build
  elif [ $EXIT_CODE -eq 101 ]; then
    echo "🧬 [Ori] Rollback Requested (Signal 101). Reverting..."
    git checkout HEAD~1
    docker compose up --build
  elif [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
    echo "🧬 [Ori] Clean shutdown. Goodbye."
    break
  else
    echo "🧬 [Ori] Daemon crashed with code $EXIT_CODE. Cool-down (30s) to avoid rate limits..."
    sleep 30
  fi
done
