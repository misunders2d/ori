#!/bin/bash
# 🧬 Ori: Manual Update (NTFS-friendly)
echo "🧬 [Ori] Forcing manual update..."
git pull
docker compose up -d --build
echo "🧬 [Ori] Sweeping old DNA..."
docker image prune -f --filter "label=project=ori" 2>/dev/null || true
echo "🧬 [Ori] Done."
