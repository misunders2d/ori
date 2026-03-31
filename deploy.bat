@echo off
rem 🧬 Ori: Manual Update (NTFS-friendly)

rem First-time interactive setup wizard
if not exist "data\.env" goto run_wizard
findstr "GOOGLE_API_KEY=" "data\.env" >nul
if %ERRORLEVEL% neq 0 goto run_wizard
goto skip_wizard

:run_wizard
echo 🧬 [Ori] First-time setup detected. Launching interactive wizard...
docker compose run --rm -it ori-agent uv run python interfaces/setup_wizard.py

:skip_wizard
echo 🧬 [Ori] Forcing manual update...
git pull
docker compose up -d --build
echo 🧬 [Ori] Sweeping old DNA...
docker image prune -f --filter "label=project=ori" >nul 2>&1
echo 🧬 [Ori] Done.
pause