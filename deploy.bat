@echo off
rem 🧬 Ori: Manual Update (NTFS-friendly)
echo 🧬 [Ori] Forcing manual update...
git pull
docker compose up -d --build
echo 🧬 [Ori] Sweeping old DNA...
docker image prune -f --filter "label=project=ori" >nul 2>&1
echo 🧬 [Ori] Done.
pause