@echo off
echo 🧬 [Ori] Manually pulling and rebuilding...
git pull
docker compose up -d --build
echo 🧬 [Ori] Done.
pause
