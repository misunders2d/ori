@echo off
rem 🧬 Ori: Windows Universal Supervisor (v3.0)
rem Hardened for Signal-Based Updates and Rate Limits.

set IMAGE_NAME=ori-agent-image

rem First-time interactive setup wizard
if not exist "data\.env" goto run_wizard
findstr "GOOGLE_API_KEY=" "data\.env" >nul
if %ERRORLEVEL% neq 0 goto run_wizard
goto skip_wizard

:run_wizard
echo 🧬 [Ori] First-time setup detected. Launching interactive wizard...
docker compose run --rm -it ori-agent uv run python interfaces/setup_wizard.py

:skip_wizard
:loop
echo 🧬 [Ori] Starting daemon...

rem Clean up dangling images from previous evolutionary builds to prevent disk bloat
docker image prune -f --filter "label=project=ori" >nul 2>&1

rem Optimization: Only --build if the image is missing or an update was requested.
rem This avoids hitting Rate Limits on every single crash/restart.
set IMG=
for /f "tokens=*" %%i in ('docker images -q %IMAGE_NAME% 2^>nul') do set IMG=%%i

if "%IMG%"=="" (
    echo 🧬 [Ori] Image missing. Building...
    docker compose up --build
) else (
    docker compose up
)

set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% equ 100 goto update
if %EXIT_CODE% equ 101 goto rollback
if %EXIT_CODE% equ 0 goto clean
if %EXIT_CODE% equ 130 goto clean

echo 🧬 [Ori] Daemon crashed (Code %EXIT_CODE%). Cool-down (30s) to avoid rate limits...
timeout /t 30
goto loop

:update
echo 🧬 [Ori] Update Requested (Signal 100). Pulling and Rebuilding...
git pull
docker compose up --build
goto loop

:rollback
echo 🧬 [Ori] Rollback Requested (Signal 101). Reverting...
git checkout HEAD~1
docker compose up --build
goto loop

:clean
echo 🧬 [Ori] Clean shutdown. Goodbye.
exit /b 0