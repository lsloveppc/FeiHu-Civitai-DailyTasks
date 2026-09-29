@echo off
chcp 65001 >nul
title Feihu Civitai Daily
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

rem ===============================================================
rem  NOTE: this script is intentionally ASCII-only.
rem  cmd parses the .bat file using the system codepage (GBK on a
rem  Chinese Windows), while chcp only changes the OUTPUT codepage.
rem  Any non-ASCII byte inside this file gets mis-decoded and can
rem  break the line into fragments that cmd then tries to execute.
rem  So: keep this file pure ASCII, and let Python print all the
rem  Chinese messages (it emits UTF-8, which displays fine after
rem  the chcp above).
rem ===============================================================

set "PY="

python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto got_python

py --version >nul 2>nul
if not errorlevel 1 set "PY=py"
if defined PY goto got_python

echo.
echo   [X] Python not found.
echo.
echo       Please install Python 3.10 or newer from:
echo         https://www.python.org/downloads/
echo       Remember to check "Add Python to PATH" during setup.
echo.
pause
exit /b 1

:got_python
%PY% -c "import fastapi, uvicorn, httpx, typer, rich, yaml" >nul 2>nul
if not errorlevel 1 goto run

echo.
echo   [!] Installing dependencies (one-time setup, please wait)...
echo.
%PY% -m pip install -r requirements.txt
if not errorlevel 1 goto run

echo.
echo   [X] Failed to install dependencies.
echo       Check your network, or run manually:
echo         pip install -r requirements.txt
echo.
pause
exit /b 1

:run
%PY% -m civitai_daily.cli launch

echo.
pause
