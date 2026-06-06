@echo off
REM Watchdog launcher — runs the Egon health watchdog detached.
REM Invoked by scheduled task "Egon-Watchdog" at logon.
cd /d "%~dp0.."
start "" /B "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0watchdog.py"
