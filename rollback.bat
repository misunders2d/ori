@echo off

set BOT_NAME=Ori
if exist "data\.env" (
    for /f "tokens=1,* delims==" %%A in ('type "data\.env" ^| findstr "^BOT_NAME="') do set BOT_NAME=%%B
)
set BOT_NAME=%BOT_NAME:"=%
rem 🧬 %BOT_NAME%: Manual Rollback Override (v3.0)
echo 🧬 [%BOT_NAME%] Rolling back to previous version...
git checkout HEAD~1
docker compose up -d --build
echo 🧬 [%BOT_NAME%] Done.
pause