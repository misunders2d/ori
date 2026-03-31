#!/bin/bash
# 🧬 Ori: Manual Update (NTFS-friendly)

# First-time interactive setup wizard
if [ ! -f "data/.env" ] || ! grep -q "GOOGLE_API_KEY=" "data/.env"; then
  echo "🧬 [Ori] First-time setup detected. Launching interactive wizard..."
  docker compose run --rm -it ori-agent uv run python interfaces/setup_wizard.py
fi

echo "🧬 [Ori] Forcing manual update..."
git pull
docker compose up -d --build
echo "🧬 [Ori] Sweeping old DNA..."
docker image prune -f --filter "label=project=ori" 2>/dev/null || true
echo "🧬 [Ori] Done."
