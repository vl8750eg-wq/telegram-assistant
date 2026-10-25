@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0env.bat"

echo === Установка окружения ===
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)
echo -- setuptools/wheel/pyaes (только готовые колёса и локальная сборка) --
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir --progress-bar off --only-binary :all: setuptools wheel
".venv\Scripts\python.exe" -m pip list --disable-pip-version-check 2>nul | findstr /i "^pyaes " >nul || (
    echo -- pyaes собирается локально --
    ".venv\Scripts\python.exe" -m pip download --disable-pip-version-check --no-deps --progress-bar off --no-binary :all: "pyaes==1.6.1" -d "%~dp0data\tmp\pyaes"
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir --progress-bar off "%~dp0data\tmp\pyaes\pyaes-1.6.1.tar.gz"
)
echo -- остальные зависимости --
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-cache-dir --progress-bar off -r requirements.txt

echo.
echo === Настройка ===
".venv\Scripts\python.exe" setup.py
echo.
echo === Вход в Telegram ===
".venv\Scripts\python.exe" login.py
echo.
echo Готово. Запуск: start.bat
pause
