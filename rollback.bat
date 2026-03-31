@echo off
echo 🧬 [Ori] Rolling back to previous version...
git checkout HEAD~1
docker compose up -d --build
echo 🧬 [Ori] Done.
pause
