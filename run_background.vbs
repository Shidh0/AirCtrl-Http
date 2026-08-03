Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyScript = Chr(34) & scriptDir & "\control.py" & Chr(34)

WshShell.Run "pythonw.exe " & pyScript, 0, False