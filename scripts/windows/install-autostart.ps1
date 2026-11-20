# Starts the Expenses app in the background whenever you sign in to Windows: no window, no terminal.
# Phones can only reach the app while it's running, so this keeps it running.
#
# Run it once yourself, from the project folder:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install-autostart.ps1
# Undo with scripts\windows\remove-autostart.ps1. It's an ordinary task for your own Windows user
# (Task Scheduler > "Expenses app"); it needs no administrator rights and changes nothing else.

$ErrorActionPreference = "Stop"
$TaskName = "Expenses app"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "Can't find $pythonw. Create the virtual environment first (see README, Quick start)."
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "app.py" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts the Expenses app (http://127.0.0.1:8765) when you sign in." -Force | Out-Null

$busy = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "Set up. The app is already running now; from the next sign-in it starts by itself."
} else {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Set up, and starting now. Open http://127.0.0.1:8765. From now on it starts by itself when you sign in."
}
Write-Host "Logs: $root\data\logs\app.log"
