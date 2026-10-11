' Air Mouse - starts the host (and the optional camera-stream server) with no window.
Option Explicit

Dim sh, fso, dir, pyw, serverDir, f, t
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = "pythonw.exe"

' Setup.py stores the exact Python that has the dependencies installed.
If fso.FileExists(dir & "\pythonw.path") Then
  On Error Resume Next
  Set f = fso.OpenTextFile(dir & "\pythonw.path", 1)
  t = Trim(f.ReadLine)
  f.Close
  If Err.Number = 0 Then
    If t <> "" Then
      If fso.FileExists(t) Then pyw = t
    End If
  End If
  Err.Clear
  On Error GoTo 0
End If

' Python host (logs to server.log next to control.py)
sh.CurrentDirectory = dir
sh.Run """" & pyw & """ """ & dir & "\control.py""", 0, False

' Optional Node.js stream server: only started if the server folder exists
serverDir = dir & "\server"
If fso.FileExists(serverDir & "\server.js") Then
  sh.CurrentDirectory = serverDir
  sh.Run "cmd /c node server.js >> """ & dir & "\node.log"" 2>&1", 0, False
End If
