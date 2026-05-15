@echo off
setlocal
title AutoPost — Instalare autostart
cd /d "%~dp0"

echo.
echo  ================================================================
echo    AutoPost — Instalare autostart la pornirea Windows
echo  ================================================================
echo.

REM --- Verifica venv ---------------------------------------------------
set PYTHONW=C:\Users\Intel\Desktop\ABCode\.venv\Scripts\pythonw.exe
if not exist "%PYTHONW%" (
    echo   [X] Nu gasesc pythonw.exe la:
    echo       %PYTHONW%
    echo       Verifica ca venv-ul exista.
    pause
    exit /b 1
)
echo   [OK] Python venv gasit.

REM --- Verifica watchdog -----------------------------------------------
if not exist "%~dp0watchdog.py" (
    echo   [X] watchdog.py nu a fost gasit.
    pause
    exit /b 1
)

REM --- Copiaza VBS in folderul Startup Windows -------------------------
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
set DST=%STARTUP%\AutoPost_Watchdog.vbs

echo   [1/3] Copiez AutoPost in Startup Windows...
copy /y "%~dp0launch_silent.vbs" "%DST%" >nul
if errorlevel 1 (
    echo   [X] Nu s-a putut copia in Startup. Incearca ca Administrator.
    pause
    exit /b 1
)
echo   [OK] Adaugat in: %DST%

REM --- Opreste instante vechi ------------------------------------------
echo   [2/3] Opresc procese AutoPost vechi (daca exista)...
taskkill /FI "IMAGENAME eq pythonw.exe" /F >nul 2>&1
timeout /t 2 /nobreak >nul

REM --- Porneste watchdog-ul acum ---------------------------------------
echo   [3/3] Pornesc AutoPost Watchdog acum...
start "" wscript.exe "%~dp0launch_silent.vbs"

timeout /t 6 /nobreak >nul
start "" "http://localhost:8000"

echo.
echo  ================================================================
echo   INSTALARE COMPLETA!
echo.
echo   Dashboard:   http://localhost:8000   (deschis automat)
echo   Autostart:   DA — porneste singur la fiecare repornire Windows
echo   Watchdog:    reporneste automat daca ceva crapa
echo.
echo   Log-uri (in folderul proiectului):
echo     watchdog.log   — reporniri automate
echo     server.log     — dashboard API
echo     scheduler.log  — joburi 08:00 / 18:00
echo.
echo   Ca sa dezinstalezi: ruleaza uninstall_autostart.bat
echo  ================================================================
echo.
pause
