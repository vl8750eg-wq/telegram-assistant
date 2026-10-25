@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Сначала выполните setup.bat
    pause
    exit /b 1
)
call "%~dp0env.bat"
".venv\Scripts\python.exe" main.py
pause
