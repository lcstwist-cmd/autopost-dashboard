@echo off
title AutoPost - Setup Wizard
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [X] Python nu e instalat sau nu e in PATH.
    echo       Instaleaza Python 3.10+ de la https://python.org
    echo       si bifeaza "Add Python to PATH" in timpul instalarii.
    echo.
    pause
    exit /b 1
)

python setup_wizard.py
pause
