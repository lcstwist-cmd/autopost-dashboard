@echo off
title AutoPost - Diagnostic Avatar AI
cd /d "%~dp0"

echo.
echo  ===============================================================
echo     AutoPost - Diagnostic complet pentru AVATAR AI
echo  ===============================================================
echo.
echo  Acest script face 3 lucruri:
echo    1. Sterge cache-ul .pyc vechi (fixuri sa prinda efect)
echo    2. Testeaza D-ID izolat  (test_did.py)
echo    3. Daca D-ID merge, reporneste recovery pe slot existent
echo.
echo  TOT output-ul se scrie si in fisiere .txt ca sa-l pot vedea.
echo.
pause

REM --- 1. Curata cache-ul .pyc -----------------------------------------
echo.
echo  [1/3] Sterg __pycache__...
powershell -NoProfile -Command "Get-ChildItem -Path . -Filter __pycache__ -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
echo        OK

REM --- 2. Testeaza D-ID izolat ----------------------------------------
echo.
echo  [2/3] Test D-ID izolat...
python test_did.py > test_did_console.txt 2>&1
type test_did_console.txt
set DID_RC=%errorlevel%
if %DID_RC% neq 0 (
    echo.
    echo  ===============================================================
    echo   X D-ID testul a esuat ^(exit code %DID_RC%^).
    echo.
    echo     Daca vezi "Unsupported file url" — D-ID refuza URL-ul
    echo     presenter-ului. Solutie: urca-ti propria poza la D-ID:
    echo.
    echo        python upload_presenter.py C:\cale\spre\poza.jpg
    echo.
    echo     Dupa upload, rerulezi diagnose_avatar.bat si gata.
    echo.
    echo     Log complet: test_did_log.txt
    echo  ===============================================================
    pause
    exit /b %DID_RC%
)
echo  ✓ D-ID functioneaza izolat.

REM --- 3. Recovery pe slot existent -----------------------------------
echo.
echo  [3/3] Rerun make_clip.py pe slotul 2026-04-23_evening...
echo        ^(daca nu exista, scoate --slot-dir si ruleaza flow complet^)
echo.
if exist "queue\2026-04-23_evening" (
    python make_clip.py --slot-dir queue\2026-04-23_evening --no-open > recovery_log.txt 2>&1
    type recovery_log.txt
) else (
    echo  Slotul queue\2026-04-23_evening nu exista.
    echo  Rulez flow complet in schimb...
    python make_clip.py --no-open > recovery_log.txt 2>&1
    type recovery_log.txt
)

echo.
echo  ===============================================================
echo    Gata. Log-uri salvate in:
echo      - test_did_log.txt       ^(test D-ID izolat^)
echo      - test_did_console.txt   ^(consola testului^)
echo      - recovery_log.txt       ^(output recovery^)
echo.
echo    Verifica: queue\2026-04-23_evening\reel_avatar.mp4
echo  ===============================================================
echo.
pause
