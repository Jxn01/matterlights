<#
.SYNOPSIS
    Registers the scheduled task that turns the lights off when Windows shuts
    down or restarts.

.DESCRIPTION
    The in-process shutdown hook (matterlights.shutdown_hook) cannot be relied
    on by itself. Measured across six shutdowns and two restarts it never fired,
    even though its window was alive and answering messages -- the process that
    owns that window is a child of the venv launcher stub and is torn down with
    it, without its message pump ever seeing WM_ENDSESSION.

    This task does not depend on the sync loop surviving. Task Scheduler starts
    a fresh process when Windows logs System event 1074 ("shutdown initiated"),
    which is raised for both shutdown and restart. On this machine roughly 15
    seconds elapse between event 1074 and the session going down; the helper
    completes in well under a second.

    Safe to re-run: registration is idempotent.

.PARAMETER Remove
    Unregister the task instead of installing it.
#>
param(
    [string]$TaskName = "MatterLights Lights Off At Shutdown",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "Scheduled task '$TaskName' is not registered; nothing to do."
    }
    return
}

if (-not (Test-Path $pythonw)) {
    throw "Virtualenv interpreter not found at $pythonw. Create the venv first."
}

# pythonw, not python: a console window flashing up mid-shutdown is ugly, and
# there is no console to read output from anyway -- the helper logs to the file.
$command = $pythonw
$arguments = "-m matterlights.lights_off"

# Runs as SYSTEM, deliberately, NOT as the logged-on user.
#
# Registered first as an interactive task, it fired correctly one second after
# event 1074 and then failed with 0xC000026B (STATUS_DLL_INIT_FAILED_LOGOFF):
# "the application failed to initialize because the window station is shutting
# down". By the time event 1074 is logged the interactive session is already
# being destroyed, and Windows will not start a new process in it -- which is
# the same reason the in-process hook never receives its session-end message.
#
# SYSTEM runs in session 0, which is not torn down at that point, so the process
# actually starts. It needs nothing from the user session: the bearer token and
# the absolute LOG_PATH both come from .env, and the request is plain HTTP.
$runAsUser = "NT AUTHORITY\SYSTEM"

# An event trigger cannot be expressed with New-ScheduledTaskTrigger, so the
# task is defined as XML. The subscription matches System log event 1074 from
# User32, which Windows raises for shutdown and restart alike.
$subscription = @"
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name='User32'] and (EventID=1074)]]</Select></Query></QueryList>
"@

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Turns the MatterLights bulbs off when Windows begins shutting down or restarting.</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>$([System.Security.SecurityElement]::Escape($subscription))</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$([System.Security.SecurityElement]::Escape($runAsUser))</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$([System.Security.SecurityElement]::Escape($command))</Command>
      <Arguments>$([System.Security.SecurityElement]::Escape($arguments))</Arguments>
      <WorkingDirectory>$([System.Security.SecurityElement]::Escape($repoRoot))</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Registering a task that runs as SYSTEM requires an elevated PowerShell. Re-run this script as administrator."
}

# -ErrorAction Stop: without it the failure is non-terminating and the script
# happily prints its success banner over the top of an "Access is denied".
Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force -ErrorAction Stop | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "  Runs:    $command $arguments"
Write-Host "  Trigger: System event 1074 (User32) - shutdown and restart"
Write-Host "  As:      $runAsUser (session 0; the user session is already gone by then)"
Write-Host ""
Write-Host "Test it without rebooting:  Start-ScheduledTask -TaskName '$TaskName'"
