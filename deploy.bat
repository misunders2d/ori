@echo off
REM Auto-deploy watcher for the Ori Daemon (Windows)
REM Triggered strictly when the agent successfully self-evolves and signals via `.update_trigger`

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "TRIGGER_FILE=%SCRIPT_DIR%\data\.update_trigger"
set "ENV_FILE=%SCRIPT_DIR%\data\.env"

REM Write our own PID for lifecycle management
for /f "tokens=2" %%a in ('tasklist /fi "PID eq %PID%" /fo list ^| findstr "PID:"') do set "SELF_PID=%%a"
REM Fallback: use PowerShell to get current PID
if not defined SELF_PID for /f %%p in ('powershell -NoProfile -Command "$PID"') do set "SELF_PID=%%p"
if defined SELF_PID echo !SELF_PID!> "%SCRIPT_DIR%\data\.deploy_watcher.pid"

echo %date% %time%: Ori Daemon Watcher Online. Monitoring for Update Signals...

:loop
if not exist "%TRIGGER_FILE%" goto :sleep

echo.
echo ==========================================
echo %date% %time%: UPDATE SIGNAL DETECTED. SEIZING HOST CONTROL.
echo ==========================================

REM Read trigger content
set "TRIGGER_CONTENT="
for /f "usebackq delims=" %%L in ("%TRIGGER_FILE%") do set "TRIGGER_CONTENT=!TRIGGER_CONTENT!%%L"
del /f "%TRIGGER_FILE%" >nul 2>nul

REM Give agent 10s to cleanly exit current API request handling
timeout /t 10 /nobreak >nul

pushd "%SCRIPT_DIR%"

REM Capture current commit (tolerate broken HEAD)
for /f "delims=" %%H in ('git rev-parse HEAD 2^>nul') do set "PREVIOUS_COMMIT=%%H"
if not defined PREVIOUS_COMMIT set "PREVIOUS_COMMIT=UNKNOWN"

echo   [+] Force-syncing to authoritative remote (origin/master)...

REM Remote is the ONLY source of truth. Nuke all local state unconditionally.
git fetch origin master >nul 2>nul
if %errorlevel% neq 0 (
    echo   [-] FATAL: Cannot reach origin. Network or remote config broken.
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Update Failed: Cannot reach git remote. Check network/SSH keys."
    popd
    goto :sleep
)

REM Wipe everything: uncommitted changes, diverged history, corrupted HEAD — all of it
git reset --hard origin/master 2>&1
git clean -fd 2>&1
echo   [+] Local branch force-aligned to origin/master.

REM Check for zero-delta signal
for /f "delims=" %%C in ('git rev-parse HEAD') do set "CURRENT_COMMIT=%%C"
git diff-index --quiet HEAD -- >nul 2>nul
if "!PREVIOUS_COMMIT!"=="!CURRENT_COMMIT!" if !errorlevel! equ 0 (
    echo   [.] Zero-delta signal. Agent architecture and local file state unchanged.
    popd
    goto :sleep
)

echo   [+] Building fresh daemon container...
docker compose build 2>&1
if %errorlevel% neq 0 (
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Compile Failure. Container failed to package. Initiating structural rollback..."
    call :rollback "!PREVIOUS_COMMIT!" "!TRIGGER_CONTENT!"
    popd
    goto :sleep
)

echo   [+] Orchestrating local cluster swap...
docker compose up -d 2>&1
if %errorlevel% neq 0 (
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Upstream Failure. Compose daemon rejected start. Initiating structural rollback..."
    call :rollback "!PREVIOUS_COMMIT!" "!TRIGGER_CONTENT!"
    popd
    goto :sleep
)

call :wait_for_healthy
if %errorlevel% equ 0 (
    echo %date% %time%: Evolution sequence closed successfully.
    call :send_notification "!TRIGGER_CONTENT!" "💠 Self-Evolution Successful. Daemon rebuilt and actively polling."
    echo   [+] Cleaning up dangling Docker build caches...
    docker image prune -f --filter "dangling=true" >nul 2>nul
) else (
    echo %date% %time%: Core loop rejection on start! Rolling back mutations.
    call :send_notification "!TRIGGER_CONTENT!" "🚨 CRITICAL: Newly compiled code crashed daemon on boot. Triggering structural rollback..."
    call :rollback "!PREVIOUS_COMMIT!" "!TRIGGER_CONTENT!"
)

echo ==========================================
popd

:sleep
timeout /t 5 /nobreak >nul
goto :loop

REM === Subroutines ===

:wait_for_healthy
echo   [.] Waiting for daemon stability check (15s)...
timeout /t 15 /nobreak >nul
for /f "delims=" %%S in ('docker inspect -f "{{.State.Running}}" ori-agent-daemon 2^>nul') do set "RUNNING=%%S"
if "!RUNNING!"=="true" (
    echo   [+] Daemon container persists and is stable.
    exit /b 0
)
echo   [-] FATAL: Container crashed on boot!
exit /b 1

:rollback
set "RB_COMMIT=%~1"
set "RB_TRIGGER=%~2"
echo   [!] Reverting broken commit on remote to prevent re-deploy loop...
git revert HEAD --no-edit 2>&1
git push origin master 2>&1
if %errorlevel% neq 0 echo   [-] WARNING: Could not push revert to remote.
docker compose build 2>&1
docker compose up -d 2>&1
call :wait_for_healthy
if %errorlevel% equ 0 (
    echo %date% %time%: Rollback successful.
    call :send_notification "%RB_TRIGGER%" "✅ Auto-rollback successful. Broken commit reverted on origin/master."
) else (
    echo %date% %time%: FATAL CASCADING FAILURE.
    call :send_notification "%RB_TRIGGER%" "🚨 FATAL: Reverted code also failed to boot. Manual intervention required."
)
exit /b 0

:send_notification
set "SN_TRIGGER=%~1"
set "SN_MESSAGE=%~2"
REM Extract notify type and chat_id from JSON trigger via Python
for /f "delims=" %%T in ('echo %SN_TRIGGER% ^| python -c "import sys,json; print(json.load(sys.stdin).get('notify',{}).get('type',''))" 2^>nul') do set "NOTIFY_TYPE=%%T"
if /i "!NOTIFY_TYPE!"=="telegram" (
    for /f "delims=" %%C in ('echo %SN_TRIGGER% ^| python -c "import sys,json; print(json.load(sys.stdin)['notify']['chat_id'])" 2^>nul') do set "CHAT_ID=%%C"
    REM Read bot token from env file
    for /f "tokens=1,* delims==" %%A in ('findstr /b "TELEGRAM_BOT_TOKEN=" "%ENV_FILE%" 2^>nul') do (
        set "BOT_TOKEN=%%B"
        set "BOT_TOKEN=!BOT_TOKEN:"=!"
    )
    if defined BOT_TOKEN if defined CHAT_ID (
        curl -s -X POST "https://api.telegram.org/bot!BOT_TOKEN!/sendMessage" -H "Content-Type: application/json" -d "{\"chat_id\": !CHAT_ID!, \"text\": \"!SN_MESSAGE!\"}" >nul 2>nul
        echo   [+] Notification sent to Telegram chat !CHAT_ID!
    )
)
exit /b 0
