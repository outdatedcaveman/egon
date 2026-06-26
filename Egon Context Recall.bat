@echo off
setlocal
set "ROOT=%~dp0"
set "PY="
set "PYHOME="
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "home" "%ROOT%.venv\pyvenv.cfg" 2^>nul') do set "PYHOME=%%B"
for /f "tokens=* delims= " %%A in ("%PYHOME%") do set "PYHOME=%%A"
if not "%PYHOME%"=="" set "PY=%PYHOME%\pythonw.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\pythonw.exe"
set "PYTHONPATH=%ROOT%.venv\Lib\site-packages;%PYTHONPATH%"
set "PYTHONDONTWRITEBYTECODE=1"
start "" "%PY%" "%ROOT%scripts\contextual_recall_popup.py"
