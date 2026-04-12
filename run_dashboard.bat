@echo off
cd /d C:\Users\pc\xauusdagent
call venv\Scripts\activate
echo.
echo  BTC Trading Dashboard
echo  Open: http://localhost:8080
echo.
python dashboard.py
pause
