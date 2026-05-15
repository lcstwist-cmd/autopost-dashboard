@echo off
title AutoPost - Generare clip cap-coada
cd /d "%~dp0"

echo.
echo  ===============================================================
echo     Crypto AutoPost - Generare clip cu avatar AI
echo     (News - Postari - Imagine - Video - Avatar AI)
echo  ===============================================================
echo.

REM --- 1. Check Python --------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] EROARE: Python nu e gasit in PATH.
    echo      Instaleaza Python 3.10+ de la https://python.org
    echo      si bifeaza "Add Python to PATH" in timpul instalarii.
    echo.
    pause
    exit /b 1
)

REM --- 2. Install deps if needed ---------------------------------------
echo  [1/3] Verific dependente Python...
python -m pip install -r requirements.txt -q --disable-pip-version-check
if errorlevel 1 (
    echo  [X] EROARE la instalarea dependentelor.
    echo      Ruleaza manual:  python -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo        OK

REM --- 3. .env check ----------------------------------------------------
if not exist .env (
    echo  [X] .env nu exista.
    echo      Copiaza .env.example in .env si pune-ti cheile:
    echo         - DID_API_KEY        ^(obligatoriu pentru avatar^)
    echo         - ANTHROPIC_API_KEY  ^(optional, imbunatateste textele^)
    echo         - TELEGRAM_BOT_TOKEN ^(optional, pentru publicare^)
    echo.
    pause
    exit /b 1
)

REM --- 4. Run the full pipeline ----------------------------------------
echo  [2/3] Pornesc pipeline-ul... ^(dureaza 2-4 minute^)
echo.
python make_clip.py %*
set PIPE_RC=%errorlevel%

if %PIPE_RC% neq 0 (
    echo.
    echo  [X] Pipeline a esuat cu codul %PIPE_RC%.
    echo      Verifica erorile de mai sus si reincearca.
    pause
    exit /b %PIPE_RC%
)

echo  [3/3] Gata. Clipul s-a deschis automat ^(daca exista^).
echo.
pause
