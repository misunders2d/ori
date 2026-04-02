@echo off
setlocal enabledelayedexpansion

set BOT_NAME=Ori
if exist "data\.env" (
    for /f "tokens=1,* delims==" %%A in ('type "data\.env" ^| findstr "^BOT_NAME="') do (
        set val=%%B
        set val=!val:"=!
        set val=!val:'=!
        set val=!val: =!
        if not "!val!"=="" set BOT_NAME=!val!
    )
)

rem 🧬 %BOT_NAME%: Windows Universal Supervisor (v5.0)
rem Hardened for Signal-Based Updates, Rate Limits, and CLI Fallback.

set IMAGE_NAME=ori-agent-image

rem --- 1. BOOTSTRAP: FIRST CONTACT ---
rem If credentials are missing, launch the interactive setup wizard inside a one-off container.
if not exist "data\.env" goto run_wizard
findstr "GOOGLE_API_KEY=" "data\.env" >nul
if %ERRORLEVEL% neq 0 goto run_wizard
goto skip_wizard

:run_wizard
echo 🧬 [%BOT_NAME%] First-time setup detected. Launching interactive wizard...
docker compose run --rm -it --entrypoint "" ori-agent uv run python interfaces/setup_wizard.py
if %ERRORLEVEL% neq 0 (
    echo ❌ Setup Wizard failed or was interrupted.
    pause
    exit /b 1
)

:skip_wizard
:loop
echo 🧬 [%BOT_NAME%] Starting daemon...

rem Clean up dangling images from previous evolutionary builds to prevent disk bloat
docker image prune -f --filter "label=project=ori" >nul 2>&1

rem Optimization: Only --build if the image is missing or dependencies changed.
set REBUILD=false
set IMG=
for /f "tokens=*" %%i in ('docker images -q %IMAGE_NAME% 2^>nul') do set IMG=%%i
if "%IMG%"=="" set REBUILD=true

if "%REBUILD%"=="true" (
    echo 🧬 [%BOT_NAME%] Image missing. Building...
    docker compose up --build
) else (
    rem Basic check for pyproject.toml changes if 'data\.last_build' exists
    set DO_BUILD=
    if exist "data\.last_build" (
        for /f "tokens=*" %%i in ('xcopy /d /y "pyproject.toml" "data\.last_build" 2^>nul ^| findstr /c:"1 File(s) copied"') do set DO_BUILD=true
    )
    
    if "!DO_BUILD!"=="true" (
        echo 🧬 [%BOT_NAME%] Dependencies changed. Rebuilding...
        docker compose up --build
        echo. > "data\.last_build"
    ) else (
        docker compose up
    )
)

set EXIT_CODE=%ERRORLEVEL%

rem --- 2. SIGNAL HANDLING ---

if %EXIT_CODE% equ 100 goto update
if %EXIT_CODE% equ 101 goto rollback
if %EXIT_CODE% equ 0 goto clean
if %EXIT_CODE% equ 130 goto clean

echo 🧬 [%BOT_NAME%] Daemon crashed (Code %EXIT_CODE%). Cool-down (30s) to avoid rate limits...
timeout /t 30
goto loop

:update
echo 🧬 [%BOT_NAME%] Update Requested (Signal 100). Synchronizing DNA...
git pull
docker compose up --build
goto loop

:rollback
echo 🧬 [%BOT_NAME%] Rollback Requested (Signal 101). Reverting DNA...
git reset --hard HEAD~1
git clean -fd --exclude=data --exclude=.env
docker compose up --build
goto loop

:clean
echo 🧬 [%BOT_NAME%] Clean shutdown. Goodbye.
exit /b 0