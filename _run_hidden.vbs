' Launch the GUI with a hidden python.exe console.
' (pythonw.exe breaks Playwright's stdio, so we use python.exe but hide its console.)
' IMPORTANT: keep this file ASCII only - Japanese characters break wscript (800A0005).
Option Explicit
Dim sh, fso, scriptDir, py, app, logf, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
py = scriptDir & "\.venv\Scripts\python.exe"
app = scriptDir & "\app.py"
logf = scriptDir & "\startup_log.txt"
cmd = "cmd /c """"" & py & """ """ & app & """ > """ & logf & """ 2>&1"""
sh.Run cmd, 0, False
