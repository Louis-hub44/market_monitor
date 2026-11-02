@echo off
REM Vérifie la configuration et teste une donnée par provider.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
market-monitor doctor --online
pause
