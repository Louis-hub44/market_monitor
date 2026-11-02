@echo off
REM Lint, typage et tests (à lancer avant chaque commit).
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
ruff check src tests || exit /b 1
mypy src || exit /b 1
pytest
