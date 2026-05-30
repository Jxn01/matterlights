param(
    [string]$SyncTaskName = "MatterLights Screen Sync",
    [string]$DashboardTaskName = "MatterLights Dashboard",
    [int]$DashboardPort = 8770
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$syncScript = Join-Path $repoRoot "scripts\start-sync.ps1"
$dashboardScript = Join-Path $repoRoot "scripts\start-dashboard.ps1"

if (-not (Test-Path $syncScript)) {
    throw "Sync start script not found at $syncScript."
}

if (-not (Test-Path $dashboardScript)) {
    throw "Dashboard start script not found at $dashboardScript."
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

$syncArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$syncScript`" -Foreground"
$syncAction = New-ScheduledTaskAction -Execute "powershell" -Argument $syncArgs -WorkingDirectory $repoRoot
$dashboardArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dashboardScript`" -Port $DashboardPort -Foreground -NoBrowser"
$dashboardAction = New-ScheduledTaskAction -Execute "powershell" -Argument $dashboardArgs -WorkingDirectory $repoRoot

Register-ScheduledTask `
    -TaskName $SyncTaskName `
    -Description "Sync Home Assistant lights to the primary screen color." `
    -Action $syncAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Register-ScheduledTask `
    -TaskName $DashboardTaskName `
    -Description "Run the MatterLights local control dashboard on localhost." `
    -Action $dashboardAction `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

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
Write-Host "Both tasks start automatically at logon. The installer also attempted to start them immediately."