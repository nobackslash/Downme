@echo off
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
echo Abrindo Downme em http://127.0.0.1:5000
start "" http://127.0.0.1:5000
.venv\Scripts\python app.py
