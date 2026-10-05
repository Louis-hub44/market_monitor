@echo off
REM Routine du matin, à planifier (Planificateur de tâches Windows, jours ouvrés 8h15) :
REM   export daily macro (Excel, PNG, texte) puis alertes. Journal : logs\market_monitor.log
REM Code retour 3 si une alerte critique est déclenchée.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
market-monitor daily-macro
if errorlevel 2 exit /b %errorlevel%
market-monitor alerts --json exports\alerts_latest.json --fail-on critical
exit /b %errorlevel%
