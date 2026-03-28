@echo off
REM Independent Rollback watcher for the Ori Daemon (Windows)
REM Triggered strictly when the agent fails or human orders a blind revert.

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "TRIGGER_FILE=%SCRIPT_DIR%\data\.rollback_trigger"
set "ENV_FILE=%SCRIPT_DIR%\data\.env"

REM Write our own PID for lifecycle management
for /f %%p in ('powershell -NoProfile -Command "$PID"') do set "SELF_PID=%%p"
if defined SELF_PID echo !SELF_PID!> "%SCRIPT_DIR%\data\.rollback_watcher.pid"

echo %date% %time%: Ori Daemon Rollback Switch Online. Monitoring For Abort Signals...

:loop
if not exist "%TRIGGER_FILE%" goto :sleep

echo.
echo ==========================================
echo %date% %time%: ROLLBACK SIGNAL DETECTED. SEIZING HOST CONTROL.
echo ==========================================

REM Read trigger content
set "TRIGGER_CONTENT="
for /f "usebackq delims=" %%L in ("%TRIGGER_FILE%") do set "TRIGGER_CONTENT=!TRIGGER_CONTENT!%%L"
del /f "%TRIGGER_FILE%" >nul 2>nul

timeout /t 10 /nobreak >nul

pushd "%SCRIPT_DIR%"

REM --- Step 1: Align to remote truth ---
echo   [+] Fetching authoritative remote state...
git fetch origin master >nul 2>nul
if %errorlevel% neq 0 (
    echo   [-] FATAL: Cannot reach origin. Rollback impossible without remote.
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Rollback Failed: Cannot reach git remote. Check network/SSH keys."
    popd
    goto :sleep
)
git reset --hard origin/master 2>&1
git clean -fd 2>&1

REM --- Step 2: Revert HEAD on remote ---
for /f "delims=" %%H in ('git rev-parse --short HEAD 2^>nul') do set "CURRENT_HEAD=%%H"
echo   [!] Reverting commit !CURRENT_HEAD! on remote...

git revert HEAD --no-edit 2>&1
if %errorlevel% neq 0 (
    echo   [-] FATAL: git revert failed (merge commit or conflict^).
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Rollback Failed: Could not cleanly revert HEAD (!CURRENT_HEAD!). Manual revert required."
    git revert --abort 2>nul
    popd
    goto :sleep
)

git push origin master 2>&1
if %errorlevel% neq 0 (
    echo   [-] FATAL: Push to remote failed.
    call :send_notification "!TRIGGER_CONTENT!" "⚠️ Rollback Failed: Revert created locally but push to origin rejected. Check remote permissions."
    popd
    goto :sleep
)
echo   [+] Revert pushed to origin/master.

REM --- Step 3: Rebuild and restart ---
echo   [+] Rebuilding daemon from reverted code...
docker compose build 2>&1
docker compose up -d 2>&1

call :wait_for_healthy
if %errorlevel% equ 0 (
    echo %date% %time%: Rollback Sequence completed.
    call :send_notification "!TRIGGER_CONTENT!" "✅ Rollback complete. Reverted !CURRENT_HEAD! on origin/master. Daemon rebuilt and stable."
    echo   [+] Cleaning up dangling Docker build caches...
    docker image prune -f --filter "dangling=true" >nul 2>nul
) else (
    echo %date% %time%: Rollback structure failed. Reverted code also crashes.
    call :send_notification "!TRIGGER_CONTENT!" "🚨 FATAL: Reverted code also crashed on boot. Manual intervention required."
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
    exit /b 0
)
exit /b 1

:send_notification
set "SN_TRIGGER=%~1"
set "SN_MESSAGE=%~2"
for /f "delims=" %%T in ('echo %SN_TRIGGER% ^| python -c "import sys,json; print(json.load(sys.stdin).get('notify',{}).get('type',''))" 2^>nul') do set "NOTIFY_TYPE=%%T"
if /i "!NOTIFY_TYPE!"=="telegram" (
    for /f "delims=" %%C in ('echo %SN_TRIGGER% ^| python -c "import sys,json; print(json.load(sys.stdin)['notify']['chat_id'])" 2^>nul') do set "CHAT_ID=%%C"
    for /f "tokens=1,* delims==" %%A in ('findstr /b "TELEGRAM_BOT_TOKEN=" "%ENV_FILE%" 2^>nul') do (
        set "BOT_TOKEN=%%B"
        set "BOT_TOKEN=!BOT_TOKEN:"=!"
    )
    if defined BOT_TOKEN if defined CHAT_ID (
        curl -s -X POST "https://api.telegram.org/bot!BOT_TOKEN!/sendMessage" -H "Content-Type: application/json" -d "{\"chat_id\": !CHAT_ID!, \"text\": \"!SN_MESSAGE!\"}" >nul 2>nul
    )
)
exit /b 0
