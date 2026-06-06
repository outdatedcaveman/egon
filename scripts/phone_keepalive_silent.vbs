Set s = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
s.Run """" & scriptDir & "\phone_keepalive.bat""", 0, False
