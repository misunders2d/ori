#!/bin/bash

# --- 1. SYSTEM IDENTITY ---
BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  # Extract BOT_NAME from .env if present
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

# Export host IDs for container permission alignment
export AGENT_UID=$(id -u)
export AGENT_GID=$(id -g)

# 🧬 $BOT_NAME: Host-side Supervisor Loop (Signal-Based) - v3.0
# Hardened for detached repos and permission stability across reboots.

IMAGE_NAME="ori-agent-image"

# --- 2. BOOTSTRAP: FIRST CONTACT ---
# If credentials are missing, launch the interactive setup wizard inside a one-off container.
if [ ! -f "data/.env" ] || ! grep -q "GOOGLE_API_KEY=" "data/.env"; then
  echo "🧬 [$BOT_NAME] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
fi

# --- 3. THE REGENERATION LOOP ---
while true; do
  echo "🧬 [$BOT_NAME] Starting daemon..."
  
  # Optimization: Only --build if the image is missing.
  # Evolution (Signal 100) will explicitly trigger a rebuild later.
  if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    echo "🧬 [$BOT_NAME] Image missing. Building..."
    docker compose up --build
  else
    # Simple start to bypass slow rebuilds on normal crashes/reboots.
    docker compose up
  fi
  
  EXIT_CODE=$?

  # --- 4. SIGNAL HANDLING ---
  
  # Exit Code 100: The agent has modified its own source code and requested a hot-reload.
  if [ $EXIT_CODE -eq 100 ]; then
    echo "🧬 [$BOT_NAME] Evolution Signal (100). Syncing and Rebuilding..."
    
    # Permission Recovery: Fix any root-owned objects in .git left by container operations.
    if [ -d ".git" ]; then
      sudo chown -R "$(id -u):$(id -g)" .git 2>/dev/null || true
    fi
    
    # Pull latest from remote (handles detached or original repos)
    if ! git pull origin master; then
      echo "🚨 [$BOT_NAME] git pull failed! Continuing with local changes."
    fi
    
    # Rebuild image to bake in the new DNA
    docker compose build --no-cache
    # Note: Loop continues and starts the new image.
    
  # Exit Code 101: The agent encountered a fatal error after a change and requested a rollback.
  elif [ $EXIT_CODE -eq 101 ]; then
    echo "🧬 [$BOT_NAME] Rollback Signal (101). Reverting to stable DNA..."
    
    if [ -d ".git" ]; then
      sudo chown -R "$(id -u):$(id -g)" .git 2>/dev/null || true
    fi
    
    git checkout HEAD~1
    docker compose build --no-cache
    
  # Clean Exit: The user stopped the bot manually.
  elif [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
    echo "🧬 [$BOT_NAME] Clean shutdown. Goodbye."
    break
    
  # Fatal Crash: Wait and restart to avoid hitting API rate limits or disk thrashing.
  else
    echo "🧬 [$BOT_NAME] Daemon crashed with code $EXIT_CODE. Cool-down (30s) to avoid rate limits..."
    # Auto-cleanup dangling builds during crash loops to save disk space
    docker image prune -f --filter "label=project=ori" 2>/dev/null || true
    sleep 30
  fi
done
