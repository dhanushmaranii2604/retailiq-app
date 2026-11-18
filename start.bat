@echo off
cd /d %~dp0
start "RetailIQ server" python -m uvicorn web_chat:app --host 0.0.0.0 --port 8000
:wait
timeout /t 2 >nul
curl -s -o nul http://localhost:8000/health
if errorlevel 1 goto wait
start http://localhost:8000