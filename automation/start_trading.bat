@echo off
REM Double-click wrapper -- .ps1 files open in a text editor on double-click
REM by Windows default, they don't execute. This runs the real script properly.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_trading.ps1"
pause
