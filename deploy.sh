#!/bin/bash
# 🧬 Ori: Manual Update (NTFS-friendly)
echo "🧬 [Ori] Forcing manual update..."
git pull
docker compose up -d --build
