@echo off
setlocal
cd /d C:\Users\pc\Desktop\AIBtclaude
if not exist logs mkdir logs
echo.
echo  BTC Trading Dashboard
echo  Open: http://localhost:8080
echo.
python dashboard.py
pause
