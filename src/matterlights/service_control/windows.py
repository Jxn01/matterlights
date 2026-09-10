"""Service control through Task Scheduler and PowerShell.

Lifted out of ``dashboard.py`` unchanged when the Linux port introduced a
service-control protocol. Every quirk documented here was already load-bearing:

* ``CREATE_NO_WINDOW`` on every call. Without it the dashboard's status poll
  flashes a console window several times a minute for as long as the page is open.
* The dashboard's self-restart goes through **WMI** (``Win32_Process.Create``)
  so the helper runs under the WMI provider host rather than as a child of this
  process. ``Stop-ScheduledTask`` terminates the whole task tree, and a child
  would be taken down before it could restart anything.
* The zone designer is launched detached, with the process-group and no-window
  flags, so it survives the dashboard and opens no console.

And one quirk that was NOT load-bearing, because it was broken:

* ``Stop-ScheduledTask`` IS ASYNCHRONOUS. It signals termination and returns
  while the process is still alive, so the obvious
  ``Stop-ScheduledTask ; Start-ScheduledTask`` starts the new instance before
  the old one has gone. The new instance then finds the single-instance mutex
  held and exits cleanly, the old one finishes dying, and the service is left
  STOPPED while Task Scheduler records ``LastTaskResult: 0`` and the dashboard
  reports success. ``restart`` therefore waits for the task to stop being
  reported as Running before starting it -- see :meth:`_wait_until_stopped`.
  The systemd backend needs none of this: ``systemctl restart`` already waits.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from matterlights.service_control import DASHBOARD, SYNC, ZONE_UI, ServiceStatus

LOGGER = logging.getLogger("matterlights.service_control.windows")

TASK_NAMES = {
    SYNC: "MatterLights Screen Sync",
    DASHBOARD: "MatterLights Dashboard",
}
ZONE_UI_MODULE = "matterlights.zone_ui"
DASHBOARD_MODULE = "matterlights.dashboard"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class WindowsServiceControl:
    # A POLL BUDGET, not a time budget: each poll spawns PowerShell (~0.5s
    # observed) on top of the sleep, so 40 polls is roughly 10-30s of wall clock
    # depending on how slow the spawns are. Deliberately generous -- the observed
    # gap between Stop-ScheduledTask returning and the process actually dying was
    # ~1.1s (two polls), but a sync loop mid Home Assistant request unwinds slower.
    _STOP_WAIT_POLLS = 40
    _STOP_POLL_SECONDS = 0.25

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER

    def start(self, service: str) -> None:
        if service == ZONE_UI:
            self._launch_zone_ui()
            return
        self._run(f"Start-ScheduledTask -TaskName '{_ps_quote(TASK_NAMES[service])}' -ErrorAction Stop")

    def stop(self, service: str) -> int:
        if service == ZONE_UI:
            return self._stop_module_processes(ZONE_UI_MODULE)
        self._run(
            f"Stop-ScheduledTask -TaskName '{_ps_quote(TASK_NAMES[service])}' -ErrorAction SilentlyContinue"
        )
        return 1

    def restart(self, service: str) -> None:
        if service == ZONE_UI:
            self._stop_module_processes(ZONE_UI_MODULE)
            self._launch_zone_ui()
            return

        self.stop(service)
        if not self._wait_until_stopped(service):
            self._logger.warning(
                "%s still reports Running after %d status polls; starting anyway. If the new "
                "instance exits immediately, the single-instance lock was still held by the old one.",
                TASK_NAMES[service],
                self._STOP_WAIT_POLLS,
            )
        self.start(service)

    def _wait_until_stopped(self, service: str) -> bool:
        """Poll until Task Scheduler stops reporting the task as Running.

        Returns True if it stopped, False if it was still Running when the
        budget ran out -- the caller starts it regardless, because refusing to
        start a service the user asked to restart is the worse failure.

        Bounded by a POLL COUNT rather than a wall-clock deadline so the
        behaviour is deterministic under test with :meth:`_sleep` stubbed out.
        """

        for _ in range(self._STOP_WAIT_POLLS):
            if self.status(service).state.lower() != "running":
                return True
            self._sleep(self._STOP_POLL_SECONDS)
        return False

    def _sleep(self, seconds: float) -> None:
        """Seam so tests can exercise the wait without actually waiting."""

        time.sleep(seconds)

    def is_running(self, service: str) -> bool:
        if service == ZONE_UI:
            return bool(self._module_processes(ZONE_UI_MODULE))
        return self.status(service).state.lower() == "running"

    def status(self, service: str) -> ServiceStatus:
        if service == ZONE_UI:
            running = bool(self._module_processes(ZONE_UI_MODULE))
            return ServiceStatus(
                exists=running, name=ZONE_UI_MODULE, state="Running" if running else "Stopped"
            )

        task_name = TASK_NAMES[service]
        script = f"""
$task = Get-ScheduledTask -TaskName '{_ps_quote(task_name)}' -ErrorAction SilentlyContinue
if ($null -eq $task) {{
  [ordered]@{{ exists = $false; taskName = '{_ps_quote(task_name)}' }} | ConvertTo-Json -Compress
  exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{_ps_quote(task_name)}'
$lastRunTime = ''
if ($info.LastRunTime -is [datetime] -and $info.LastRunTime -ne [datetime]::MinValue) {{
  $lastRunTime = ([datetime]$info.LastRunTime).ToString('s')
}}
$nextRunTime = ''
if ($info.NextRunTime -is [datetime] -and $info.NextRunTime -ne [datetime]::MinValue) {{
  $nextRunTime = ([datetime]$info.NextRunTime).ToString('s')
}}
[ordered]@{{
  exists = $true
  taskName = $task.TaskName
  state = [string]$task.State
  lastRunTime = $lastRunTime
  nextRunTime = $nextRunTime
  lastTaskResult = $info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
        payload = self._run_json(script)
        if not isinstance(payload, dict) or not payload.get("exists"):
            return ServiceStatus(exists=False, name=task_name)
        return ServiceStatus(
            exists=True,
            name=str(payload.get("taskName", task_name)),
            state=str(payload.get("state", "")),
            last_run=str(payload.get("lastRunTime", "")),
            next_run=str(payload.get("nextRunTime", "")),
            last_result=str(payload.get("lastTaskResult", "")),
        )

    def restart_self(self, service: str) -> None:
        task = _ps_quote(TASK_NAMES[service])
        module = _ps_quote(DASHBOARD_MODULE)
        dashboard_script = _ps_quote(str(_repo_root() / "scripts" / "start-dashboard.ps1"))
        script = f"""
Start-Sleep -Seconds 1
$task = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
if ($null -ne $task) {{ Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue }}
Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m {module}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Milliseconds 800
if ($null -ne $task) {{
  Start-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
}} else {{
  & '{dashboard_script}' -NoBrowser
}}
"""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        helper_command = f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
        launcher = (
            "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = '{_ps_quote(helper_command)}' }} | Out-Null"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launcher],
            cwd=_repo_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    # -- helpers -----------------------------------------------------------

    def _launch_zone_ui(self) -> None:
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            [sys.executable, "-m", ZONE_UI_MODULE],
            cwd=_repo_root(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def _module_processes(self, module_name: str) -> list[dict[str, Any]]:
        script = f"""
$matches = Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m {_ps_quote(module_name)}*' }} | Select-Object ProcessId, CommandLine
if ($null -eq $matches) {{
  '[]'
  exit 0
}}
$matches | ConvertTo-Json -Compress
"""
        result = self._run_json(script)
        if result is None:
            return []
        if isinstance(result, dict):
            return [result]
        return result

    def _stop_module_processes(self, module_name: str) -> int:
        script = f"""
$matches = Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m {_ps_quote(module_name)}*' }}
$ids = @($matches | Select-Object -ExpandProperty ProcessId)
foreach ($id in $ids) {{
  Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}}
[ordered]@{{ count = $ids.Count }} | ConvertTo-Json -Compress
"""
        result = self._run_json(script)
        if isinstance(result, dict):
            return int(result.get("count", 0))
        return 0

    def _run_json(self, script: str) -> Any:
        stdout = self._run(script).stdout.strip()
        if not stdout:
            return None
        return json.loads(stdout)

    def _run(self, script: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=_repo_root(),
            capture_output=True,
            # Windows PowerShell writes a pipe in [Console]::OutputEncoding, the
            # console's OEM code page (850 on a Western-European install) -- not
            # the ANSI one Python would assume (1252). The two disagree on every
            # accented letter, and cp1252 has no character at all for five OEM
            # bytes, the u-umlaut among them. Decode as OEM; never let a stray
            # byte raise out of a status poll.
            encoding="oem",
            errors="replace",
            check=False,
            # Without this the status poll flashes a console window several
            # times a minute for as long as the dashboard page is open.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "PowerShell command failed."
            raise RuntimeError(message)
        return result


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")
