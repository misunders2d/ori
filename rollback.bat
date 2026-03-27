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

REM Pop previous stable commit off local head securely
for /f "skip=1 delims=" %%H in ('git log -2 --format^="%%H"') do set "PREVIOUS_COMMIT=%%H"

echo   [!] Rolling back single evolution node to !PREVIOUS_COMMIT!...
git reset --hard "!PREVIOUS_COMMIT!"

REM Clear out remote tracking temporarily so next update works properly
git fetch origin master >nul 2>&1

echo   [+] Force compiling previous stable...
docker compose build 2>&1
docker compose up -d 2>&1

call :wait_for_healthy
if %errorlevel% equ 0 (
    echo %date% %time%: Rollback Sequence completed.
    call :send_notification "!TRIGGER_CONTENT!" "✅ Rollback Sequence Complete. Daemon stabilized onto previous Git signature."
    echo   [+] Cleaning up dangling Docker build caches...
    docker image prune -f --filter "dangling=true" >nul 2>nul
) else (
    echo %date% %time%: Rollback structure failed. Re-initiating container cycle...
    call :send_notification "!TRIGGER_CONTENT!" "🚨 FATAL: Previous stable code also crashed on boot. Manual intervention required!"
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
