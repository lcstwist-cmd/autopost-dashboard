@echo off
title AutoPost — Dezinstalare autostart
cd /d "%~dp0"

echo.
echo  Opresc AutoPost si scot din autostart Windows...
echo.

REM Opreste watchdog + procese copil
taskkill /FI "IMAGENAME eq pythonw.exe" /F >nul 2>&1
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq AutoPost*" /F >nul 2>&1

REM Sterge VBS din Startup
set VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AutoPost_Watchdog.vbs
if exist "%VBS%" (
    del /f "%VBS%"
    echo   [OK] Scos din Startup Windows.
) else (
    echo   [--] Nu era in Startup.
)

echo.
echo  Gata. AutoPost nu mai porneste automat la repornire.
echo  Ruleaza install_autostart.bat ca sa reactivezi.
echo.
pause
