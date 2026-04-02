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

# 🧬 $BOT_NAME: Host-side Supervisor Loop (Signal-Based) - v4.0
# Hardened for detached repos and forced remote synchronization.

IMAGE_NAME="ori-agent-image"

# --- 2. BOOTSTRAP: FIRST CONTACT ---
# If credentials are missing, launch the interactive setup wizard inside a one-off container.
# Check for GOOGLE_API_KEY in file OR environment
if [ ! -f "data/.env" ] || { ! grep -q "GOOGLE_API_KEY=" "data/.env" && [ -z "$GOOGLE_API_KEY" ]; }; then
  echo "🧬 [$BOT_NAME] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
fi

# --- 3. THE REGENERATION LOOP ---
while true; do
  echo "🧬 [$BOT_NAME] Starting daemon..."
  
  # Optimization: Only --build if the image is missing or dependencies changed.
  # We check if pyproject.toml is newer than the image's creation time (approx by checking a local stamp)
  REBUILD=false
  if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    REBUILD=true
  fi
  
  if [ "$REBUILD" = true ]; then
    echo "🧬 [$BOT_NAME] Image missing or update required. Building..."
    docker compose up --build
  else
    # Check if pyproject.toml or Dockerfile is newer than data/.last_build
    if [ "pyproject.toml" -nt "data/.last_build" ] || [ "Dockerfile" -nt "data/.last_build" ]; then
        echo "🧬 [$BOT_NAME] Dependencies or Dockerfile changed. Rebuilding..."
        docker compose up --build
        touch data/.last_build
    else
        docker compose up
    fi
  fi
  
  EXIT_CODE=$?

  # --- 4. SIGNAL HANDLING ---
  
  # Exit Code 100: Evolution Signal (The agent modified its code)
  if [ $EXIT_CODE -eq 100 ]; then
    echo "🧬 [$BOT_NAME] Evolution Signal (100). Executing Forced Sync & Rebuild..."
    
    # Permission Recovery: Fix any root-owned objects in .git left by container operations.
    if [ -d ".git" ]; then
      sudo chown -R "$(id -u):$(id -g)" .git 2>/dev/null || true
    fi
    
    # FORCED SYNC: Destroy any local drift/conflicts and match the remote master exactly.
    echo "🧬 [$BOT_NAME] Synchronizing host DNA with remote master..."
    git fetch origin master
    git reset --hard origin/master
    # Safer clean: explicitly exclude data directory and hidden env files
    git clean -fd --exclude=data --exclude=.env
    
    # Rebuild image from the fresh DNA
    docker compose build --no-cache
    touch data/.last_build
    
  # Exit Code 101: Rollback Signal (Fatal error detected)
  elif [ $EXIT_CODE -eq 101 ]; then
    echo "🧬 [$BOT_NAME] Rollback Signal (101). Reverting to previous DNA..."
    
    if [ -d ".git" ]; then
      sudo chown -R "$(id -u):$(id -g)" .git 2>/dev/null || true
    fi
    
    git reset --hard HEAD~1
    git clean -fd --exclude=data --exclude=.env
    docker compose build --no-cache
    touch data/.last_build
    
  # Clean Exit: The user stopped the bot manually.
  elif [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 130 ]; then
    echo "🧬 [$BOT_NAME] Clean shutdown. Goodbye."
    break
    
  # Fatal Crash: Wait and restart to avoid hitting API rate limits or disk thrashing.
  else
    echo "🧬 [$BOT_NAME] Daemon crashed with code $EXIT_CODE. Cool-down (30s)..."
    docker image prune -f --filter "label=project=ori" 2>/dev/null || true
    sleep 30
  fi
done
