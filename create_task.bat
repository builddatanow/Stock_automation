@echo off
schtasks /create /tn "TradingWatchdog" /tr "\"C:\Program Files\Python311\python.exe\" \"C:\Users\Administrator\Desktop\projects\watchdog.py\"" /sc onstart /ru Administrator /rl highest /f
echo Done.
pause
