#!/bin/bash

# --- 1. SYSTEM IDENTITY ---
BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  # Extract BOT_NAME from .env if present
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '"'\''\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || echo "")
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi

# Export host IDs for container permission alignment
export AGENT_UID=$(id -u)
export AGENT_GID=$(id -g)

# 🧬 $BOT_NAME: Host-side Supervisor Loop (Signal-Based) - v5.0 (Rootless Edition)
# Hardened for read-only containers. Evolution is pushed to Remote and pulled to Host.

IMAGE_NAME="ori-agent-image"
CRASH_FILE="data/.crash_count"
MAX_CRASHES=3

# --- 2. BOOTSTRAP: FIRST CONTACT ---
# If credentials are missing, launch the interactive setup wizard inside a one-off container.
if [ ! -f "data/.env" ] || { ! grep -q "GOOGLE_API_KEY=" "data/.env" && [ -z "$GOOGLE_API_KEY" ]; }; then
  echo "🧬 [$BOT_NAME] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
fi

# --- 3. THE REGENERATION LOOP ---
while true; do
  # --- HOST-SIDE WATCHDOG: RECOVERY MECHANISM ---
  # If the bot is caught in a crash loop, roll back the code on the host.
  if [ -f "$CRASH_FILE" ]; then
    CRASHES=$(cat "$CRASH_FILE")
    if [[ "$CRASHES" =~ ^[0-9]+$ ]] && [ "$CRASHES" -ge "$MAX_CRASHES" ]; then
        echo "🚨 [$BOT_NAME Host] Detected $CRASHES consecutive crashes. Initiating Emergency Rollback..."
        
        # In Rootless Mode, .git is not mounted, so permissions should remain stable.
        git reset --hard HEAD~1
        git clean -fd --exclude=data --exclude=.env
        echo 0 > "$CRASH_FILE"
        echo "🧬 [$BOT_NAME Host] Rollback complete. Rebuilding..."
        docker compose up --build
        EXIT_CODE=$?
        continue
    fi
  fi

  echo "🧬 [$BOT_NAME] Starting daemon..."
  
  # Optimization: Only --build if the image is missing or dependencies changed.
  REBUILD=false
  if [[ "$(docker images -q $IMAGE_NAME 2> /dev/null)" == "" ]]; then
    REBUILD=true
  fi
  
  # Determine if we should run in interactive CLI mode (no messenger tokens found)
  USE_CLI=true
  if grep -qE "^(TELEGRAM_BOT_TOKEN|SLACK_BOT_TOKEN)=" data/.env 2>/dev/null; then
    USE_CLI=false
  fi

  if [ "$USE_CLI" = true ]; then
    echo "🧬 [$BOT_NAME] No messenger configured. Launching in interactive CLI mode..."
    # Start dependencies in background
    docker compose up -d cloudflare-tunnel 2>/dev/null
    # Run agent service interactively
    docker compose run --rm -it --service-ports ori-agent
  else
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
  fi
  
  EXIT_CODE=$?

  # --- 4. SIGNAL HANDLING ---
  
  # Exit Code 100: Evolution Signal (The agent modified its code on Remote)
  if [ $EXIT_CODE -eq 100 ]; then
    echo "🧬 [$BOT_NAME] Evolution Signal (100). Executing Forced Sync & Rebuild..."
    
    echo "🧬 [$BOT_NAME] Synchronizing host DNA with remote master..."
    git fetch origin master
    git reset --hard origin/master
    git clean -fd --exclude=data --exclude=.env
    
    docker compose build --no-cache
    touch data/.last_build
    echo 0 > "$CRASH_FILE"
    
  # Exit Code 101: Manual Rollback Signal
  elif [ $EXIT_CODE -eq 101 ]; then
    echo "🧬 [$BOT_NAME] Rollback Signal (101). Reverting to previous DNA..."
    
    git reset --hard HEAD~1
    git clean -fd --exclude=data --exclude=.env
    docker compose build --no-cache
    touch data/.last_build
    echo 0 > "$CRASH_FILE"
    
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
