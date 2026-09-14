@echo off
rem Opens a public HTTPS tunnel (cloudflared) to the locally running bot, so Telegram can
rem open the Mini App from this computer. Run start.bat (the bot) first in another window.
rem Comments are ASCII on purpose: cmd.exe reads .bat files in the OEM codepage.
cd /d "%~dp0"

chcp 65001 >nul
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. See README.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" serve.py

echo.
echo --- tunnel stopped ---
pause
