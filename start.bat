@echo off
rem 🧬 Ori: Windows Universal Supervisor (v3.0)
rem Hardened for Signal-Based Updates

echo 🧬 [Ori] Windows Universal Supervisor Initialized.

:loop
echo 🧬 [Ori] Starting daemon...
docker compose up
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% equ 100 (
    echo 🧬 [Ori] UPDATE SIGNAL RECEIVED (100).
    git pull
    echo 🧬 [Ori] Rebuilding container...
    docker compose up --build -d
    echo 🧬 [Ori] Update complete. Restarting in 5s...
    timeout /t 5
    goto loop
)
if %EXIT_CODE% equ 101 (
    echo 🧬 [Ori] ROLLBACK SIGNAL RECEIVED (101).
    git checkout HEAD~1
    echo 🧬 [Ori] Rebuilding previous version...
    docker compose up --build -d
    timeout /t 5
    goto loop
)
if %EXIT_CODE% equ 0 (
    echo 🧬 [Ori] Clean shutdown.
    exit /b 0
)
if %EXIT_CODE% equ 130 (
    echo 🧬 [Ori] Interrupt received (Ctrl+C).
    exit /b 0
)

echo 🧬 [Ori] Daemon crashed (Code %EXIT_CODE%). Restarting in 10s...
timeout /t 10
goto loop
