@echo off
REM Installation (Windows) : environnement virtuel, dépendances, fichier .env.
cd /d "%~dp0\.."
if not exist .venv (
    py -3.11 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e ".[dev]"
if not exist .env copy .env.example .env
echo.
echo Installation terminee. Renseigner FMP_API_KEY dans .env, puis lancer scripts\doctor.bat
