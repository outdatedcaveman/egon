@echo off
REM Egon launcher — visible console (debug). For silent launch use "Launch Egon.vbs".
cd /d "%~dp0"
".venv\Scripts\python.exe" egon_launcher.py
