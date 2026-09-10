param(
    [string]$SyncTaskName = "MatterLights Screen Sync",
    [string]$DashboardTaskName = "MatterLights Dashboard"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$envFile = Join-Path $repoRoot ".env"

# The tasks run pythonw directly, and pythonw has no console to report a
# failure on -- so the checks start-sync.ps1 and start-dashboard.ps1 make at
# every start are made here, once, instead.
if (-not (Test-Path $pythonw)) {
    throw "Virtualenv interpreter not found at $pythonw. Create the venv and install the package first."
}
if (-not (Test-Path $envFile)) {
    throw "Missing .env at $envFile. Run guided setup first or create the file manually."
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
# No time limit. Left unset, Task Scheduler stops a task 72 hours after its logon
# trigger started it: a sync left running through a three-day session would just
# stop, and its own log would simply end. PT0S is "no limit" in the task XML.
$settings.ExecutionTimeLimit = "PT0S"
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

# pythonw, not a hidden PowerShell. Windows 11's default console is Windows
# Terminal, which ignores -WindowStyle Hidden and draws the window anyway: the
# earlier form of this script, powershell -WindowStyle Hidden -File start-*.ps1,
# put a terminal on the taskbar at every logon. pythonw is a GUI-subsystem
# binary and never has a console, so there is no window to hide. Startup errors
# go to the log -- LOG_PATH in .env, or %LOCALAPPDATA%\matterlights\matterlights.log
# -- and the dashboard's port is DASHBOARD_PORT in .env, since a task cannot
# pass an environment variable.
$syncAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m matterlights" -WorkingDirectory $repoRoot
$dashboardAction = New-ScheduledTaskAction -Execute $pythonw -Argument "-m matterlights.dashboard" -WorkingDirectory $repoRoot

# -ErrorAction Stop on each: Register-ScheduledTask reports a failure as a
# non-terminating error, and without it an "Access is denied" scrolls past with
# the success message printed right under it.
Register-ScheduledTask `
    -TaskName $SyncTaskName `
    -Description "Sync Home Assistant lights to the primary screen color." `
    -Action $syncAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force -ErrorAction Stop | Out-Null

Register-ScheduledTask `
    -TaskName $DashboardTaskName `
    -Description "Run the MatterLights local control dashboard on localhost." `
    -Action $dashboardAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force -ErrorAction Stop | Out-Null

try {
    Start-ScheduledTask -TaskName $SyncTaskName -ErrorAction Stop
}
catch {
    Write-Host "Sync task '$SyncTaskName' could not be started immediately: $($_.Exception.Message)"
}

try {
    Start-ScheduledTask -TaskName $DashboardTaskName -ErrorAction Stop
}
catch {
    Write-Host "Dashboard task '$DashboardTaskName' could not be started immediately: $($_.Exception.Message)"
}

Write-Host "Installed scheduled tasks '$SyncTaskName' and '$DashboardTaskName' for $currentUser."
Write-Host "Both run .venv\Scripts\pythonw.exe at logon. A copy that was already running keeps its old definition until it next starts; log off and on to be sure."
