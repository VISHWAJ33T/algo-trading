# Starts the trading bot (src.main) and the dashboard (src.dashboard) as
# hidden background processes, then opens the dashboard in the default
# browser. Invoked hidden by "Start Trading Bot.vbs" -- not meant to be run
# directly by the client.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms

$projectDir = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
$dashboardUrl = "http://127.0.0.1:8765"

function Show-Message($text, $title = "Nifty Bot") {
    [System.Windows.Forms.MessageBox]::Show($text, $title) | Out-Null
}

if (-not (Test-Path $python)) {
    Show-Message "Setup needed before this can run.`n`nExpected to find:`n$python`n`nFollow the setup steps in README.md first (create the virtual environment and install requirements), then try again." "Nifty Bot - Setup Needed"
    exit
}

$alreadyRunning = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*$projectDir*" -and $_.CommandLine -like "*src.main*"
}
if ($alreadyRunning) {
    Show-Message "The trading bot is already running. Opening the dashboard for you."
    Start-Process $dashboardUrl
    exit
}

Start-Process -FilePath $python -ArgumentList "-m", "src.main" -WorkingDirectory $projectDir -WindowStyle Hidden
Start-Process -FilePath $python -ArgumentList "-m", "src.dashboard" -WorkingDirectory $projectDir -WindowStyle Hidden

# Wait for the dashboard to actually be ready rather than guessing a fixed delay.
$deadline = (Get-Date).AddSeconds(20)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -Uri $dashboardUrl -UseBasicParsing -TimeoutSec 1 | Out-Null
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

Start-Process $dashboardUrl

if (-not $ready) {
    Show-Message "The dashboard is taking longer than expected to start. It should appear in your browser shortly -- if it doesn't within a minute or two, check logs\bot.log for an error (a common cause is incomplete details in the .env file)."
}
