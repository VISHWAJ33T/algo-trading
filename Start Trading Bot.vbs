' Double-click this file to start the trading bot and dashboard.
' No command prompt window will appear -- your browser will open the
' dashboard automatically once it's ready.

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)
scriptPath = projectDir & "\scripts\start-bot.ps1"

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & q & scriptPath & q
shell.Run cmd, 0, True
