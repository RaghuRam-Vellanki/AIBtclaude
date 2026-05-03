@echo off
setlocal
cd /d C:\Users\pc\Desktop\AIBtclaude
if not exist logs mkdir logs
python agent.py >> logs\agent.log 2>&1
