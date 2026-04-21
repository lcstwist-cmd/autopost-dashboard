@echo off
title AutoPost Dashboard
cd /d "%~dp0"

echo.
echo   AutoPost — Pornire locala
echo   ═══════════════════════════════════════
echo.

REM Use the Python from PATH (or venv if activated)
where python >nul 2>&1
if errorlevel 1 (
    echo   EROARE: Python nu a fost gasit in PATH.
    echo   Instaleaza Python 3.10+ de la python.org
    pause
    exit /b 1
)

REM Install dependencies silently if needed
echo   Verificare dependente...
python -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo   EROARE la instalarea dependentelor.
    pause
    exit /b 1
)

REM Create DB + admin user on first run
if not exist autopost.db (
    echo   Prima rulare — creare baza de date...
    python seed_db.py
)

REM Start the full launcher (uvicorn + ngrok)
python run_local.py

pause
