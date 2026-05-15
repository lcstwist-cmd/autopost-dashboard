@echo off
setlocal
title AutoPost — Pornire
cd /d "%~dp0"

echo.
echo  ===============================================================
echo    AutoPost — Pornesc dashboard + scheduler
echo  ===============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo   [X] Python nu e instalat sau nu e in PATH.
    pause & exit /b 1
)

if not exist .env (
    echo   [X] .env nu exista. Ruleaza intai setup_wizard.bat
    pause & exit /b 1
)

echo   Verific dependentele...
python -m pip install -r requirements.txt -q --disable-pip-version-check 2>nul

echo   Pornesc AutoPost Watchdog (dashboard + scheduler non-stop)...
start "" wscript.exe "%~dp0launch_silent.vbs"

timeout /t 5 /nobreak >nul
start "" "http://localhost:8000"

echo.
echo  ===============================================================
echo   AutoPost ruleaza in background.
echo   Dashboard:  http://localhost:8000
echo.
echo   Log-uri:  watchdog.log / server.log / scheduler.log
echo.
echo   Pentru autostart la repornire Windows: install_autostart.bat
echo  ===============================================================
echo.
pause
