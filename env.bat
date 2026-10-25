@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem На этой машине %TEMP% не всегда создаёт рабочий каталог для pip/python.
rem Перенаправляем TEMP в локальную папку проекта и создаём её заранее.
if not exist "%~dp0data\tmp" mkdir "%~dp0data\tmp"
set TEMP=%~dp0data\tmp
set TMP=%~dp0data\tmp
set PYTHONIOENCODING=utf-8
