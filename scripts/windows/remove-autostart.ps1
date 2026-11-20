# Stops the Expenses app from starting by itself at sign-in (undoes install-autostart.ps1).
#   powershell -ExecutionPolicy Bypass -File scripts\windows\remove-autostart.ps1
# The copy running right now keeps running until you sign out or restart.

$ErrorActionPreference = "Stop"
$TaskName = "Expenses app"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed. The app no longer starts by itself."
} else {
    Write-Host "Nothing to remove: the app wasn't set to start by itself."
}
