@echo off
rem Launcher for Karman (demo crypto wallet bot). Double-click to run.
rem Comments are ASCII on purpose: cmd.exe reads .bat files in the OEM codepage.
cd /d "%~dp0"

chcp 65001 >nul
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run this once:
    echo     py -3.14 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found. Copy .env.example to .env and set BOT_TOKEN.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot.py

echo.
echo --- bot stopped ---
pause
