@echo off
timeout /t 2 /nobreak >nul
taskkill /PID 5000 /F /T >nul 2>&1
start "" "C:\Users\Intel\AppData\Local\Programs\Python\Python311\python.exe" "C:\Users\Intel\Documents\Claude\Projects\AutoPost for Instagram , X , tiktok , telegram and youtube\src\dashboard\app.py"
