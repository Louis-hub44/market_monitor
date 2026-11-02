@echo off
REM Lance le dashboard dans le navigateur.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
market-monitor ui
