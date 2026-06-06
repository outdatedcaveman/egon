@echo off
REM Silent launcher for the phone-keepalive daemon. Installed in the
REM Windows Startup folder so it runs on every logon.
start "" /B "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0phone_keepalive.py"
