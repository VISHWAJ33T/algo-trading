' Double-click this file to stop the trading bot and dashboard.
' No command prompt window will appear.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)
scriptPath = projectDir & "\scripts\stop-bot.ps1"

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & q & scriptPath & q
shell.Run cmd, 0, True
