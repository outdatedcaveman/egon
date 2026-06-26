' Egon silent launcher — no console window.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
py = scriptDir & "\.venv\Scripts\pythonw.exe"
cfg = scriptDir & "\.venv\pyvenv.cfg"
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
env = "set PYTHONPATH=" & scriptDir & "\.venv\Lib\site-packages;%PYTHONPATH% && set PYTHONDONTWRITEBYTECODE=1 && "
sh.Run "cmd /c " & env & """" & py & """ """ & scriptDir & "\egon_launcher.py""", 0, False
