param(
    [string]$SyncTaskName = "MatterLights Screen Sync",
    [string]$DashboardTaskName = "MatterLights Dashboard"
)

$ErrorActionPreference = "Stop"

foreach ($taskName in @($SyncTaskName, $DashboardTaskName)) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Task '$taskName' is not installed."
        continue
    }

    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed scheduled task '$taskName'."
}