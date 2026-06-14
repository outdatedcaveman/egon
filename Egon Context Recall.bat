@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"
start "" "%PY%" "%ROOT%scripts\contextual_recall_popup.py"
