# Stops the trading bot (src.main) and dashboard (src.dashboard) if running.
# Invoked hidden by "Stop Trading Bot.vbs" -- not meant to be run directly.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$projectDir = Split-Path $PSScriptRoot -Parent

function Show-Message($text, $title = "Nifty Bot") {
    [System.Windows.Forms.MessageBox]::Show($text, $title) | Out-Null
}

$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*$projectDir*" -and (
        $_.CommandLine -like "*src.main*" -or $_.CommandLine -like "*src.dashboard*"
    )
}

if (-not $procs) {
    Show-Message "Nothing appears to be running right now."
    exit
}

foreach ($proc in $procs) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Show-Message "The trading bot and dashboard have been stopped."
