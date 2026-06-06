' Egon silent launcher — no console window.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
sh.Run """" & scriptDir & "\.venv\Scripts\pythonw.exe"" """ & scriptDir & "\egon_launcher.py""", 0, False

