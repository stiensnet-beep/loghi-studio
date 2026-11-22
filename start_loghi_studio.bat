@echo off
rem Start Loghi-studio (if not running yet) and open it in the browser.
set PYTHONIOENCODING=utf-8
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8771 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -WindowStyle Hidden python -ArgumentList '\"%~dp0server.py\"','8771' -RedirectStandardError '%~dp0server.err' }"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8771/
