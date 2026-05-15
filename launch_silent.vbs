' AutoPost Watchdog — lansare silentioasa (fara consola)
Dim pythonw : pythonw = "C:\Users\Intel\Desktop\ABCode\.venv\Scripts\pythonw.exe"
Dim projdir : projdir = "C:\Users\Intel\Documents\Claude\Projects\AutoPost for Instagram , X , tiktok , telegram and youtube"
Dim script  : script  = projdir & "\watchdog.py"

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = projdir
sh.Run """" & pythonw & """ """ & script & """", 0, False
