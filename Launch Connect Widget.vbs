' Egon Connect Widget — floating "connect my writing to my archives" panel.
'
' Works WITHOUT Chrome: reads the focused window's actual text content via
' Windows UI Automation (selection > focused field > window text — no
' screenshots, no OCR) and queries the local mind's Connection Engine
' (POST 127.0.0.1:8000/api/v1/mind/connect, semantic index).
'
' Global hotkey once running: Ctrl+Alt+Space — capture what you're
' reading/writing anywhere (Word, editors, PDFs, browsers) and surface
' connections from your archives in an always-on-top panel.
'
' Runs hidden via pythonw (no console window). Double-click to start.
' To auto-start at login, copy this file into:
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\bruno\Claude Code\egon"
sh.Run """C:\Users\bruno\Claude Code\egon\.venv\Scripts\pythonw.exe"" ""C:\Users\bruno\Claude Code\egon\scripts\connect_widget.py""", 0, False
