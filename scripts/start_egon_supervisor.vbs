Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
exe = root & "\bin\EgonSupervisor.exe"
sh.Run """" & exe & """ --root """ & root & """", 0, False
