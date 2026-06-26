' Egon Connect Widget — floating "connect my writing to my archives" panel.
'
' Works WITHOUT Chrome: reads the focused window's actual text content via
' Windows UI Automation (selection > focused field > window text — no
' screenshots, no OCR) and queries the local mind's Connection Engine
' (POST 127.0.0.1:8000/api/v1/mind/connect, semantic index).
'
' Global hotkey once running: Ctrl+Alt+E — freezes the screen into a dimmed
' overlay; drag to select any region (Circle-to-Search style). The region is
' OCR'd with Windows' built-in engine and connected to your archives.
' Enter/double-click = whole screen. Esc = cancel.
' Hotkey configurable in egon-config.json: {"connect_widget":{"hotkey":"..."}}
'
' Runs hidden via pythonw (no console window). Double-click to start.
' To auto-start at login, copy this file into:
'   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = root
py = root & "\.venv\Scripts\pythonw.exe"
cfg = root & "\.venv\pyvenv.cfg"
If fso.FileExists(cfg) Then
  Set file = fso.OpenTextFile(cfg, 1)
  Do Until file.AtEndOfStream
    line = Trim(file.ReadLine)
    If LCase(Replace(line, " ", "")) Like "home=*" Then
      home = Trim(Split(line, "=", 2)(1))
      basePy = home & "\pythonw.exe"
      If fso.FileExists(basePy) Then py = basePy
      Exit Do
    End If
  Loop
  file.Close
End If
env = "set PYTHONPATH=" & root & "\.venv\Lib\site-packages;%PYTHONPATH% && set PYTHONDONTWRITEBYTECODE=1 && "
sh.Run "cmd /c " & env & """" & py & """ """ & root & "\scripts\connect_widget.py""", 0, False
