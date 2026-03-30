@echo off
REM Ori Daemon — Setup & Launch (Windows)
REM Usage:
REM   start.bat [--no-sync]

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM ---------------------------------------------------------------------------
REM Colors via ANSI (Windows 10+ Terminal)
REM ---------------------------------------------------------------------------
for /f %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
set "BOLD=%ESC%[1m"
set "DIM=%ESC%[2m"
set "RESET=%ESC%[0m"
set "RED=%ESC%[31m"
set "GREEN=%ESC%[32m"
set "YELLOW=%ESC%[33m"
set "CYAN=%ESC%[36m"
set "WHITE=%ESC%[97m"
set "HR=%DIM%────────────────────────────────────────────────────────%RESET%"

REM ---------------------------------------------------------------------------
REM Banner
REM ---------------------------------------------------------------------------
echo.
echo %CYAN%%BOLD%         ██████╗ ██████╗ ██╗%RESET%
echo %CYAN%%BOLD%        ██╔═══██╗██╔══██╗██║%RESET%
echo %CYAN%%BOLD%        ██║   ██║██████╔╝██║%RESET%
echo %CYAN%%BOLD%        ██║   ██║██╔══██╗██║%RESET%
echo %CYAN%%BOLD%        ╚██████╔╝██║  ██║██║%RESET%
echo %CYAN%%BOLD%         ╚═════╝ ╚═╝  ╚═╝╚═╝%RESET%
echo %CYAN%     Autonomous Self-Evolving Agent%RESET%
echo.
echo %HR%

REM ---------------------------------------------------------------------------
REM Disclaimer
REM ---------------------------------------------------------------------------
echo.
echo   %YELLOW%%BOLD%DISCLAIMER%RESET%
echo.
echo   %DIM%Ori is an autonomous agent that can modify its own code,%RESET%
echo   %DIM%access external APIs, and manage scheduled tasks.%RESET%
echo.
echo   %DIM%This system is designed for users with technical knowledge —%RESET%
echo   %DIM%people who understand not to commit credentials to git, who%RESET%
echo   %DIM%can read logs, and who know their way around a terminal.%RESET%
echo.
echo   %DIM%That said, anyone is welcome to try it out.%RESET%
echo.
echo   %DIM%The author(s) bear no liability for data loss, unexpected%RESET%
echo   %DIM%behavior, or costs incurred through API usage. Every effort%RESET%
echo   %DIM%is made to harden security and protect your privacy, but%RESET%
echo   %DIM%this software is provided as-is, without warranty.%RESET%
echo.
echo   %DIM%By continuing, you accept these terms.%RESET%
echo.
echo %HR%
echo.
set /p "=  %WHITE%Press Enter to continue (or Ctrl+C to abort)...%RESET% " <nul
pause >nul
echo.

REM ---------------------------------------------------------------------------
REM Prerequisite checks
REM ---------------------------------------------------------------------------
echo.
echo   %BOLD%Checking prerequisites%RESET%
echo.

set "prereq_ok=1"

where docker >nul 2>nul
if %errorlevel% equ 0 (
    echo   %GREEN%✓%RESET% docker
) else (
    echo   %RED%✗%RESET% docker — not found. Please install it.
    set "prereq_ok=0"
)

where git >nul 2>nul
if %errorlevel% equ 0 (
    echo   %GREEN%✓%RESET% git
) else (
    echo   %RED%✗%RESET% git — not found. Please install it.
    set "prereq_ok=0"
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    echo   %GREEN%✓%RESET% python
) else (
    echo   %RED%✗%RESET% python — not found. Please install it.
    set "prereq_ok=0"
)

docker compose version >nul 2>nul
if %errorlevel% equ 0 (
    echo   %GREEN%✓%RESET% docker compose v2
) else (
    echo   %RED%✗%RESET% docker compose v2 — not available
    set "prereq_ok=0"
)

docker info >nul 2>nul
if %errorlevel% equ 0 (
    echo   %GREEN%✓%RESET% docker daemon running
) else (
    echo   %RED%✗%RESET% docker daemon — not running or insufficient permissions
    set "prereq_ok=0"
)

echo.
if "!prereq_ok!"=="0" (
    echo   %RED%✗%RESET% Missing prerequisites. Please install the above and retry.
    exit /b 1
)
echo %HR%

REM ---------------------------------------------------------------------------
REM Git sync
REM ---------------------------------------------------------------------------
echo.
if /i "%~1"=="--no-sync" (
    echo   %CYAN%→%RESET% Skipping remote git sync (--no-sync)
) else (
    git remote get-url origin >nul 2>nul
    if errorlevel 1 (
        echo   %CYAN%→%RESET% No remote 'origin' configured — skipping sync
        echo   %YELLOW%⚠%RESET% Add a remote later: git remote add origin ^<url^>
    ) else (
        echo   %CYAN%→%RESET% Fetching and syncing origin/master...
        git fetch origin master 2>&1
        if errorlevel 1 (
            echo   %YELLOW%⚠%RESET% Could not fetch from origin — continuing with local code
        ) else (
            git reset --hard origin/master 2>&1
            echo   %GREEN%✓%RESET% Codebase synced
        )
    )
)
echo.
echo %HR%

REM ---------------------------------------------------------------------------
REM Prepare data directory
REM ---------------------------------------------------------------------------
if not exist "%SCRIPT_DIR%\data" mkdir "%SCRIPT_DIR%\data"

REM ---------------------------------------------------------------------------
REM First-time setup wizard
REM ---------------------------------------------------------------------------
set "ENV_FILE=%SCRIPT_DIR%\data\.env"
set "bot_name=Ori"

echo.
echo   %CYAN%%BOLD%Configuration%RESET%
echo.

REM --- Google API Key (required) ---
call :env_get "GOOGLE_API_KEY" "%ENV_FILE%" existing_google_key
if "!existing_google_key!"=="" (
    echo   %WHITE%Google API Key%RESET% %RED%(required)%RESET%
    echo   %DIM%Get one at https://aistudio.google.com/apikey%RESET%
    set /p "google_key=    GOOGLE_API_KEY: "
    if not "!google_key!"=="" (
        echo GOOGLE_API_KEY="!google_key!">> "%ENV_FILE%"
        echo   %GREEN%✓%RESET% Saved
    ) else (
        echo   %YELLOW%⚠%RESET% Skipped — Ori cannot function without this key.
        echo   %YELLOW%⚠%RESET% Add it later to data\.env
    )
    echo.
) else (
    echo   %GREEN%✓%RESET% GOOGLE_API_KEY already configured
)

REM --- Bot Name (optional, default Ori) ---
call :env_get "BOT_NAME" "%ENV_FILE%" existing_bot_name
if "!existing_bot_name!"=="" (
    echo.
    echo   %WHITE%Bot Name%RESET% %DIM%(default: Ori)%RESET%
    echo   %DIM%Give your agent a unique identity.%RESET%
    set /p "bot_name=    Name: "
    if "!bot_name!"=="" set "bot_name=Ori"
    echo BOT_NAME="!bot_name!">> "%ENV_FILE%"
    echo   %GREEN%✓%RESET% Named: %BOLD%!bot_name!%RESET%
    echo.
) else (
    set "bot_name=!existing_bot_name!"
    echo   %GREEN%✓%RESET% BOT_NAME already set: %BOLD%!existing_bot_name!%RESET%
)

REM --- GitHub Repo (optional, recommended) ---
call :env_get "GITHUB_REPO" "%ENV_FILE%" existing_github_repo
if "!existing_github_repo!"=="" (
    echo.
    echo   %WHITE%GitHub Repository%RESET% %YELLOW%(recommended)%RESET%
    echo   %DIM%Enables self-evolution commits and version control.%RESET%
    echo   %DIM%Format: owner/repo (e.g., yourname/ori-instance)%RESET%
    set /p "github_repo=    GITHUB_REPO: "
    if not "!github_repo!"=="" (
        echo GITHUB_REPO="!github_repo!">> "%ENV_FILE%"
        echo   %GREEN%✓%RESET% Saved: !github_repo!
    ) else (
        echo   %CYAN%→%RESET% Skipped — you can add this later via data\.env
    )
    echo.
) else (
    echo   %GREEN%✓%RESET% GITHUB_REPO already set: !existing_github_repo!
)

REM --- Telegram Bot Token (optional, recommended) ---
call :env_get "TELEGRAM_BOT_TOKEN" "%ENV_FILE%" existing_tg_token
if "!existing_tg_token!"=="" (
    echo.
    echo   %WHITE%Telegram Bot Token%RESET% %YELLOW%(recommended)%RESET%
    echo   %DIM%Primary way to interact with your agent.%RESET%
    echo   %DIM%Create a bot via @BotFather on Telegram.%RESET%
    set /p "tg_token=    TELEGRAM_BOT_TOKEN: "
    if not "!tg_token!"=="" (
        echo TELEGRAM_BOT_TOKEN="!tg_token!">> "%ENV_FILE%"
        echo   %GREEN%✓%RESET% Saved
    ) else (
        echo   %CYAN%→%RESET% Skipped — Ori will start in CLI-only mode
    )
    echo.
) else (
    echo   %GREEN%✓%RESET% TELEGRAM_BOT_TOKEN already configured
)

REM --- TOTP 2FA (optional) ---
call :env_get "ADMIN_TOTP_SECRET" "%ENV_FILE%" existing_totp
if "!existing_totp!"=="" (
    echo.
    echo   %WHITE%Two-Factor Authentication%RESET% %DIM%(optional)%RESET%
    echo   %DIM%Add TOTP (Google Authenticator, Authy, etc.) for admin actions.%RESET%
    set /p "enable_totp=    Enable 2FA? [y/N]: "
    if /i "!enable_totp!"=="y" (
        REM Generate TOTP secret via Python
        for /f "delims=" %%S in ('python -c "import secrets, base64; print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip('='))"') do set "totp_secret=%%S"
        set "totp_issuer=!bot_name!"
        set "totp_uri=otpauth://totp/!totp_issuer!:admin?secret=!totp_secret!^&issuer=!totp_issuer!^&digits=6^&period=30"

        echo.
        echo   %GREEN%✓%RESET% TOTP secret generated
        echo.

        REM Try Python qrcode module for QR display, install if missing
        set "qr_displayed=0"
        python -c "import qrcode" 2>nul || (
            echo   %CYAN%→%RESET% Installing qrcode package for QR display...
            python -m pip install --quiet qrcode 2>nul || pip install --quiet qrcode 2>nul
        )
        python -c "import sys; import qrcode; qr = qrcode.QRCode(version=1, box_size=1, border=2); qr.add_data(sys.argv[1]); qr.make(fit=True); qr.print_ascii(invert=True)" "!totp_uri!" 2>nul && set "qr_displayed=1"

        if "!qr_displayed!"=="0" (
            echo   %YELLOW%Could not display QR code.%RESET%
            echo   %DIM%Manually add this to your authenticator app:%RESET%
        ) else (
            echo.
            echo   %DIM%Or enter manually:%RESET%
        )

        echo.
        echo   %WHITE%Account:%RESET%  !totp_issuer!:admin
        echo   %WHITE%Secret:%RESET%   %GREEN%!totp_secret!%RESET%
        echo   %WHITE%Type:%RESET%     TOTP  ^|  %WHITE%Digits:%RESET% 6  ^|  %WHITE%Period:%RESET% 30s
        echo.

        REM Verify the user has successfully added the secret to their app
        echo   %WHITE%Verify setup:%RESET% enter the 6-digit code from your authenticator app.
        echo.
        set "totp_verified=0"
        for /L %%A in (1,1,3) do (
            if "!totp_verified!"=="0" (
                set /p "totp_code=    Code: "
                python -c "import sys,base64,hashlib,hmac,struct,time;secret=sys.argv[1];code=sys.argv[2].strip();sys.exit(1) if len(code)!=6 or not code.isdigit() else None;padding=8-(len(secret)%%8);secret+='='*padding if padding!=8 else '';key=base64.b32decode(secret.upper());step=int(time.time())//30;[sys.exit(0) for off in (-1,0,1) if hmac.compare_digest(str((struct.unpack('>I',(d:=hmac.new(key,struct.pack('>Q',step+off),hashlib.sha1).digest())[(o:=d[-1]&0x0F):o+4])[0]&0x7FFFFFFF)%%1000000).zfill(6),code)];sys.exit(1)" "!totp_secret!" "!totp_code!" 2>nul
                if !errorlevel! equ 0 (
                    echo ADMIN_TOTP_SECRET="!totp_secret!">> "%ENV_FILE%"
                    echo   %GREEN%✓%RESET% Code verified — 2FA is active
                    set "totp_verified=1"
                ) else (
                    if %%A lss 3 (
                        set /a "next=%%A+1"
                        echo   %YELLOW%⚠%RESET% Invalid code. Try again ^(attempt !next!/3^).
                    )
                )
            )
        )

        if "!totp_verified!"=="0" (
            echo   %RED%✗%RESET% Could not verify TOTP code after 3 attempts.
            echo   %CYAN%→%RESET% 2FA not enabled — you can re-enable by re-running setup.
        )
        echo.
    ) else (
        echo   %CYAN%→%RESET% Skipped — you can enable this later
        echo.
    )
) else (
    echo   %GREEN%✓%RESET% TOTP already configured
)

REM --- Admin Passcode (auto-generated if missing) ---
call :env_get "ADMIN_PASSCODE" "%ENV_FILE%" existing_passcode
if "!existing_passcode!"=="" (
    for /f "delims=" %%P in ('python -NoProfile -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)))"') do set "admin_pass=%%P"
    echo ADMIN_PASSCODE="!admin_pass!">> "%ENV_FILE%"
    echo.
    echo   %WHITE%%BOLD%Admin Passcode (auto-generated)%RESET%
    echo.
    echo   %GREEN%%BOLD%  !admin_pass!%RESET%
    echo.
    echo   %YELLOW%⚠%RESET% Save this passcode securely. It will not be displayed again.
    echo   %YELLOW%⚠%RESET% Needed for initial admin authentication with the bot.
    echo.
)

echo %HR%

REM ---------------------------------------------------------------------------
REM Migrate legacy database if needed
REM ---------------------------------------------------------------------------
if exist "%SCRIPT_DIR%\data\ori.db" if not exist "%SCRIPT_DIR%\data\ori-sessions.db" (
    echo.
    echo   %CYAN%→%RESET% Migrating legacy database...
    python "%SCRIPT_DIR%\scripts\migrate_split_db.py"
    echo   %GREEN%✓%RESET% Database migrated
    echo.
)

REM ---------------------------------------------------------------------------
REM Launch container stack
REM ---------------------------------------------------------------------------
echo.
echo   %BOLD%Building ^& launching%RESET%
echo.

echo   %CYAN%→%RESET% Tearing down old instances...
docker compose down 2>&1

echo.
echo   %CYAN%→%RESET% Building fresh image (no cache)...
docker compose build --no-cache --pull 2>&1

echo.
echo   %CYAN%→%RESET% Starting container...
docker compose up -d 2>&1

echo.
echo   %CYAN%→%RESET% Cleaning up old images...
docker image prune -af --filter "label!=com.docker.compose.project" >nul 2>&1
docker container prune -f >nul 2>&1
echo   %GREEN%✓%RESET% Container stack is running
echo.
echo %HR%

REM ---------------------------------------------------------------------------
REM Restart host watchers
REM ---------------------------------------------------------------------------
set "DEPLOY_PID_FILE=%SCRIPT_DIR%\data\.deploy_watcher.pid"
set "ROLLBACK_PID_FILE=%SCRIPT_DIR%\data\.rollback_watcher.pid"

for %%F in ("%DEPLOY_PID_FILE%" "%ROLLBACK_PID_FILE%") do (
    if exist "%%~F" (
        set /p OLD_PID=<"%%~F"
        taskkill /PID !OLD_PID! /F >nul 2>nul
        del /f "%%~F" >nul 2>nul
    )
)

start "" /b cmd /c ""%SCRIPT_DIR%\deploy.bat" >> "%SCRIPT_DIR%\data\deploy.log" 2>&1"
start "" /b cmd /c ""%SCRIPT_DIR%\rollback.bat" >> "%SCRIPT_DIR%\data\rollback.log" 2>&1"

REM ---------------------------------------------------------------------------
REM Post-launch summary
REM ---------------------------------------------------------------------------
echo.
echo   %GREEN%%BOLD%!bot_name! is alive.%RESET%
echo.
echo %HR%
echo.
echo   %BOLD%Quick Reference%RESET%
echo.
echo   %WHITE%Logs%RESET%          docker logs -f ori-agent-daemon
echo   %WHITE%Deploy log%RESET%    type data\deploy.log
echo   %WHITE%Rollback log%RESET%  type data\rollback.log
echo   %WHITE%Stop%RESET%          docker compose down
echo   %WHITE%Config%RESET%        data\.env
echo.
echo %HR%
echo.
echo   %YELLOW%%BOLD%ADMIN AUTHENTICATION%RESET%
echo.
echo   %DIM%The system needs your user ID to grant admin access.%RESET%
echo.
echo   %WHITE%1.%RESET% Send any message to the bot on Telegram
echo   %WHITE%2.%RESET% The bot will reject you and show your ID %DIM%(e.g., tg_12345678)%RESET%
echo   %WHITE%3.%RESET% Send this command to the bot:
echo.
echo      %CYAN%/init ^<PASSCODE^> ADMIN_USER_IDS="tg_YOUR_ID"%RESET%
echo.
echo   %DIM%Forgot your passcode? Check data\.env (never share it).%RESET%
echo.
echo %HR%
echo.

endlocal
exit /b 0

REM ---------------------------------------------------------------------------
REM Helper: read a value from .env file
REM   call :env_get "KEY_NAME" "path\to\.env" result_var
REM ---------------------------------------------------------------------------
:env_get
set "%~3="
if not exist "%~2" exit /b 0
for /f "usebackq tokens=1,* delims==" %%A in ("%~2") do (
    if "%%A"=="%~1" (
        set "tmpval=%%B"
        REM Strip surrounding quotes
        if defined tmpval (
            set "tmpval=!tmpval:"=!"
            set "%~3=!tmpval!"
        )
    )
)
exit /b 0
