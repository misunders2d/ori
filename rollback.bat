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

rem 🧬 %BOT_NAME%: Manual Rollback Override (v5.0)
echo 🧬 [%BOT_NAME%] Rolling back to previous DNA version...
git reset --hard HEAD~1
git clean -fd --exclude=data --exclude=.env
docker compose up -d --build
echo 🧬 [%BOT_NAME%] Done.
pause