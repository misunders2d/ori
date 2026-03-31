@echo off

set BOT_NAME=Ori
if exist "data\.env" (
    for /f "tokens=1,* delims==" %%A in ('type "data\.env" ^| findstr "^BOT_NAME="') do set BOT_NAME=%%B
)
set BOT_NAME=%BOT_NAME:"=%
rem 🧬 %BOT_NAME%: Manual Update (NTFS-friendly)

rem First-time interactive setup wizard
if not exist "data\.env" goto run_wizard
findstr "GOOGLE_API_KEY=" "data\.env" >nul
if %ERRORLEVEL% neq 0 goto run_wizard
goto skip_wizard

:run_wizard
echo 🧬 [%BOT_NAME%] First-time setup detected. Launching interactive wizard...
docker compose run --rm -it ori-agent uv run python interfaces/setup_wizard.py

:skip_wizard
echo 🧬 [%BOT_NAME%] Forcing manual update...
git pull
docker compose up -d --build
echo 🧬 [%BOT_NAME%] Sweeping old DNA...
docker image prune -f --filter "label=project=ori" >nul 2>&1
echo 🧬 [%BOT_NAME%] Done.
pause