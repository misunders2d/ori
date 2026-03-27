@echo off
REM Ori Daemon Setup & Launch script (Windows)
REM Usage:
REM   start.bat [--no-sync]

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo ==========================================
echo     Ori Daemon — Setup ^& Launch Core
echo ==========================================
echo.

REM --- Check Prerequisites ---
where docker >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: 'docker' is not installed. Please install it on the host.
    exit /b 1
)

where git >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: 'git' is not installed. Please install it on the host.
    exit /b 1
)

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: 'python' is not installed. Please install it on the host.
    exit /b 1
)

docker compose version >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: 'docker compose' (v2) not available.
    exit /b 1
)

docker info >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Docker daemon is not running, or you lack permissions.
    exit /b 1
)

REM --- Sync remote codebase ---
if /i "%~1"=="--no-sync" (
    echo   [.] Skipping remote git sync...
) else (
    echo   [+] Fetching and syncing origin master...
    git fetch origin master 2>&1
    git reset --hard origin/master 2>&1
)

REM --- Prepare data buffers ---
if not exist "%SCRIPT_DIR%\data" mkdir "%SCRIPT_DIR%\data"

REM --- First-Time Configuration Wizard ---
set "ENV_FILE=%SCRIPT_DIR%\data\.env"
if not exist "%ENV_FILE%" (
    echo ==========================================
    echo   First-Time Setup Wizard
    echo ==========================================
    echo Let's configure your environment keys. Press Enter to skip if adding manually later.

    set /p "google_key=Enter GOOGLE_API_KEY: "
    set /p "tg_key=Enter TELEGRAM_BOT_TOKEN: "

    REM Auto-generate a secure 16-character alphanumeric passcode via PowerShell
    for /f "delims=" %%P in ('powershell -NoProfile -Command "$chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'; -join (1..16 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })"') do set "admin_pass=%%P"

    echo   [+] Auto-generated SECURE ADMIN_PASSCODE: !admin_pass!
    echo   [!] SAVE THIS PASSCODE SECURELY. IT WILL NOT BE DISPLAYED AGAIN.

    (
        echo GOOGLE_API_KEY="!google_key!"
        echo TELEGRAM_BOT_TOKEN="!tg_key!"
        echo ADMIN_PASSCODE="!admin_pass!"
    ) > "%ENV_FILE%"

    echo   [+] %ENV_FILE% generated securely.
    echo.
)

REM --- Migrate legacy single-database if needed ---
if exist "%SCRIPT_DIR%\data\ori.db" if not exist "%SCRIPT_DIR%\data\ori-sessions.db" (
    echo   [+] Migrating ori.db into separate session/scheduler databases...
    python "%SCRIPT_DIR%\scripts\migrate_split_db.py"
)

REM --- Launch Container Stack ---
echo   [+] Tearing down old instances and rebuilding...
docker compose down
docker compose up --build -d

REM --- Stop any old watcher processes ---
set "DEPLOY_PID_FILE=%SCRIPT_DIR%\data\.deploy_watcher.pid"
set "ROLLBACK_PID_FILE=%SCRIPT_DIR%\data\.rollback_watcher.pid"

for %%F in ("%DEPLOY_PID_FILE%" "%ROLLBACK_PID_FILE%") do (
    if exist "%%~F" (
        set /p OLD_PID=<"%%~F"
        taskkill /PID !OLD_PID! /F >nul 2>nul
        del /f "%%~F" >nul 2>nul
    )
)

REM --- Start watcher processes in background ---
start "" /b cmd /c ""%SCRIPT_DIR%\deploy.bat" >> "%SCRIPT_DIR%\data\deploy.log" 2>&1"
start "" /b cmd /c ""%SCRIPT_DIR%\rollback.bat" >> "%SCRIPT_DIR%\data\rollback.log" 2>&1"

REM Capture the PID of the most recently started background processes isn't trivially
REM possible in batch. The watchers self-register their PIDs in their own scripts.

echo.
echo ==========================================
echo   Ori is Active ^& Isolated
echo ==========================================
echo   Logs:        docker logs -f ori-agent-daemon
echo   Deploy:      type data\deploy.log
echo   Rollback:    type data\rollback.log
echo   Stop Core:   docker compose down
echo ==========================================
echo   [ACTION REQUIRED] ADMIN AUTHENTICATION
echo   The system requires your Admin ID to unlock the agent.
echo   1. Send any message to the bot on Telegram.
echo   2. The bot will reject you and reveal your ID (e.g., tg_12345678)
echo   3. Copy your ID exactly (including the 'tg_' prefix) and send this:
echo.
echo   /init "<YOUR_ADMIN_PASSCODE>" ADMIN_USER_IDS="tg_12345678"
echo.
echo   (If you forgot your generated passcode, check data\.env securely)
echo ==========================================
echo.

endlocal
