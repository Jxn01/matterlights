from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from flask import Flask, Response, jsonify, request
import requests

from matterlights.config import Settings, load_settings


APP = Flask(__name__)
SYNC_TASK_NAME = "MatterLights Screen Sync"
DASHBOARD_TASK_NAME = "MatterLights Dashboard"
ZONE_UI_MODULE = "matterlights.zone_ui"
REPO_ROOT = Path(__file__).resolve().parents[2]


@APP.errorhandler(RuntimeError)
@APP.errorhandler(ValueError)
def handle_api_error(exc: Exception) -> tuple[Response | str, int]:
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": str(exc)}), 500
    return f"<pre>{escape(str(exc))}</pre>", 500


@APP.get("/")
def index() -> str:
    return _page_html()


@APP.get("/api/status")
def get_status() -> Response:
    settings = load_settings()
    return jsonify(_build_dashboard_status(settings))


@APP.get("/api/logs")
def get_logs() -> Response:
    settings = load_settings()
    log_path = settings.log_path
    return jsonify(
        {
            "path": str(log_path) if log_path is not None else "",
            "exists": bool(log_path and log_path.exists()),
            "text": _tail_log(log_path),
        }
    )


@APP.post("/api/actions/sync/start")
def start_sync() -> Response:
    _run_powershell(
        f"Start-ScheduledTask -TaskName '{_ps_quote(SYNC_TASK_NAME)}' -ErrorAction Stop"
    )
    return jsonify({"ok": True, "message": "Started screen sync task."})


@APP.post("/api/actions/sync/stop")
def stop_sync() -> Response:
    _run_powershell(
        f"Stop-ScheduledTask -TaskName '{_ps_quote(SYNC_TASK_NAME)}' -ErrorAction SilentlyContinue"
    )
    return jsonify({"ok": True, "message": "Stopped screen sync task."})


@APP.post("/api/actions/sync/restart")
def restart_sync() -> Response:
    _run_powershell(
        " ; ".join(
            [
                f"Stop-ScheduledTask -TaskName '{_ps_quote(SYNC_TASK_NAME)}' -ErrorAction SilentlyContinue",
                f"Start-ScheduledTask -TaskName '{_ps_quote(SYNC_TASK_NAME)}' -ErrorAction Stop",
            ]
        )
    )
    return jsonify({"ok": True, "message": "Restarted screen sync task."})


@APP.post("/api/actions/zone-ui/start")
def start_zone_ui() -> Response:
    settings = load_settings()
    if _module_processes(ZONE_UI_MODULE):
        return jsonify({"ok": True, "message": "Zone designer is already running."})

    _launch_zone_ui_process(settings)
    return jsonify({"ok": True, "message": "Started zone designer."})


@APP.post("/api/actions/zone-ui/stop")
def stop_zone_ui() -> Response:
    stopped = _stop_module_processes(ZONE_UI_MODULE)
    return jsonify({"ok": True, "message": f"Stopped {stopped} zone designer process(es)."})


@APP.post("/api/actions/zone-ui/restart")
def restart_zone_ui() -> Response:
    settings = load_settings()
    stopped = _stop_module_processes(ZONE_UI_MODULE)
    _launch_zone_ui_process(settings)
    return jsonify({"ok": True, "message": f"Restarted zone designer after stopping {stopped} process(es)."})


def main() -> int:
    settings = load_settings()
    APP.run(host="127.0.0.1", port=settings.dashboard_port, debug=False, use_reloader=False)
    return 0


def _build_dashboard_status(settings: Settings) -> dict[str, Any]:
    return {
        "syncTask": _task_status(SYNC_TASK_NAME),
        "dashboardTask": _task_status(DASHBOARD_TASK_NAME),
        "zoneUi": {
            "url": f"http://127.0.0.1:{settings.zone_ui_port}",
            "port": settings.zone_ui_port,
            "processes": _module_processes(ZONE_UI_MODULE),
        },
        "homeAssistant": _home_assistant_status(settings),
        "config": {
            "dashboardUrl": f"http://127.0.0.1:{settings.dashboard_port}",
            "dashboardPort": settings.dashboard_port,
            "colorSyncMode": settings.color_sync_mode,
            "sampleStride": settings.sample_stride,
            "parallelUpdates": settings.max_parallel_light_updates,
            "lightCount": len(settings.light_entities),
            "screenCaptureTarget": settings.screen_capture_target,
        },
    }


def _launch_zone_ui_process(settings: Settings) -> None:
    env = os.environ.copy()
    env["ZONE_UI_PORT"] = str(settings.zone_ui_port)
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
  ) | getattr(
    subprocess, "CREATE_NO_WINDOW", 0
    )
    subprocess.Popen(
        [sys.executable, "-m", ZONE_UI_MODULE],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _task_status(task_name: str) -> dict[str, Any]:
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
    result = _run_powershell_json(script)
    return result if isinstance(result, dict) else {"exists": False, "taskName": task_name}


def _module_processes(module_name: str) -> list[dict[str, Any]]:
    script = f"""
$matches = Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m { _ps_quote(module_name) }*' }} | Select-Object ProcessId, CommandLine
if ($null -eq $matches) {{
  '[]'
  exit 0
}}
$matches | ConvertTo-Json -Compress
"""
    result = _run_powershell_json(script)
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    return result


def _stop_module_processes(module_name: str) -> int:
    script = f"""
$matches = Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m { _ps_quote(module_name) }*' }}
$ids = @($matches | Select-Object -ExpandProperty ProcessId)
foreach ($id in $ids) {{
  Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
}}
[ordered]@{{ count = $ids.Count }} | ConvertTo-Json -Compress
"""
    result = _run_powershell_json(script)
    if isinstance(result, dict):
        return int(result.get("count", 0))
    return 0


def _home_assistant_status(settings: Settings) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{settings.ha_url}/api/states",
            headers={
                "Authorization": f"Bearer {settings.ha_token}",
                "Content-Type": "application/json",
            },
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return {
            "reachable": False,
            "configuredLightCount": len(settings.light_entities),
            "availableLightCount": 0,
            "unavailableEntityIds": settings.light_entities,
            "error": str(exc),
        }

    wanted = set(settings.light_entities)
    available = []
    for state in response.json():
        entity_id = state.get("entity_id")
        if entity_id in wanted and state.get("state") != "unavailable":
            available.append(entity_id)

    unavailable = [entity_id for entity_id in settings.light_entities if entity_id not in available]
    return {
        "reachable": True,
        "configuredLightCount": len(settings.light_entities),
        "availableLightCount": len(available),
        "unavailableEntityIds": unavailable,
    }


def _tail_log(log_path: Path | None, max_lines: int = 120) -> str:
    if log_path is None or not log_path.exists():
        return "No log file found."
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Failed to read log file: {exc}"
    return "\n".join(lines[-max_lines:]) if lines else "Log file is empty."


def _run_powershell_json(script: str) -> Any:
    result = _run_powershell(script)
    stdout = result.stdout.strip()
    if not stdout:
        return None
    return json.loads(stdout)


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "PowerShell command failed."
        raise RuntimeError(message)
    return result


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def _page_html() -> str:
    title = escape("MatterLights Control")
    return (
        """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: rgba(17, 24, 39, 0.92);
      --line: rgba(148, 163, 184, 0.22);
      --text: #ecf4ff;
      --muted: #92a6c0;
      --accent: #7dd3fc;
      --good: #34d399;
      --warn: #f59e0b;
      --bad: #fb7185;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(125, 211, 252, 0.16), transparent 28%),
        radial-gradient(circle at bottom right, rgba(245, 158, 11, 0.14), transparent 24%),
        linear-gradient(180deg, #05070c 0%, #0d1117 100%);
      min-height: 100vh;
    }
    .shell {
      max-width: 1320px;
      margin: 0 auto;
      padding: 28px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: end;
      margin-bottom: 22px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 34px;
      letter-spacing: -0.04em;
    }
    .subtle {
      margin: 0;
      color: var(--muted);
      line-height: 1.55;
      max-width: 760px;
    }
    .links {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    a, button {
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
    }
    a.primary, button.primary {
      color: #08131c;
      background: linear-gradient(135deg, var(--accent), #c4b5fd);
    }
    button.secondary {
      color: var(--text);
      background: rgba(148, 163, 184, 0.14);
      border: 1px solid var(--line);
    }
    .status-bar {
      min-height: 24px;
      color: var(--muted);
      margin-bottom: 18px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(12, minmax(0, 1fr));
      gap: 16px;
    }
    .card {
      grid-column: span 4;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
    }
    .card.wide {
      grid-column: span 6;
    }
    .card.full {
      grid-column: 1 / -1;
    }
    .eyebrow {
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 12px;
      margin-bottom: 8px;
    }
    .title-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }
    .title-row strong {
      font-size: 20px;
    }
    .badge {
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
    }
    .badge.good {
      background: rgba(52, 211, 153, 0.14);
      color: var(--good);
    }
    .badge.warn {
      background: rgba(245, 158, 11, 0.14);
      color: var(--warn);
    }
    .badge.bad {
      background: rgba(251, 113, 133, 0.14);
      color: var(--bad);
    }
    .meta {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 14px;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    .mono {
      font-family: Consolas, "Cascadia Mono", monospace;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(2, 6, 23, 0.72);
      border: 1px solid rgba(148, 163, 184, 0.16);
      border-radius: 18px;
      padding: 16px;
      min-height: 240px;
      max-height: 440px;
      overflow: auto;
      color: #dbeafe;
    }
    @media (max-width: 1100px) {
      .card, .card.wide {
        grid-column: span 6;
      }
    }
    @media (max-width: 760px) {
      .shell {
        padding: 16px;
      }
      .hero {
        flex-direction: column;
        align-items: start;
      }
      .card, .card.wide {
        grid-column: 1 / -1;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div>
        <h1>MatterLights Control</h1>
        <p class="subtle">Monitor the screen sync task, Home Assistant reachability, the zone designer process, and recent logs. Use this page to restart the parts that matter without opening Task Scheduler or PowerShell.</p>
      </div>
      <div class="links">
        <a id="dashboardLink" class="primary" href="#">Dashboard</a>
        <a id="zoneUiLink" class="primary" href="#" target="_blank" rel="noreferrer">Open Zone Designer</a>
      </div>
    </section>
    <div id="statusBar" class="status-bar">Loading dashboard status...</div>
    <section class="grid">
      <article id="syncCard" class="card"></article>
      <article id="dashboardCard" class="card"></article>
      <article id="zoneUiCard" class="card"></article>
      <article id="haCard" class="card wide"></article>
      <article id="configCard" class="card wide"></article>
      <article class="card full">
        <div class="title-row">
          <strong>Recent Log</strong>
          <button id="refreshLogsButton" class="secondary" type="button">Refresh Log</button>
        </div>
        <pre id="logOutput">Loading log...</pre>
      </article>
    </section>
  </div>
  <script>
    const statusBar = document.getElementById('statusBar');
    const syncCard = document.getElementById('syncCard');
    const dashboardCard = document.getElementById('dashboardCard');
    const zoneUiCard = document.getElementById('zoneUiCard');
    const haCard = document.getElementById('haCard');
    const configCard = document.getElementById('configCard');
    const logOutput = document.getElementById('logOutput');
    const dashboardLink = document.getElementById('dashboardLink');
    const zoneUiLink = document.getElementById('zoneUiLink');

    function badge(state, fallback = 'Unknown') {
      const value = (state || fallback).toString();
      const normalized = value.toLowerCase();
      let cls = 'warn';
      if (['running', 'ready', 'reachable', 'active', 'live'].includes(normalized)) cls = 'good';
      if (['disabled', 'stopped', 'missing', 'failed', 'unreachable'].includes(normalized)) cls = 'bad';
      return `<span class="badge ${cls}">${value}</span>`;
    }

    function metaLine(label, value) {
      return `<div><strong>${label}:</strong> <span class="mono">${value || '—'}</span></div>`;
    }

    function renderTaskCard(container, title, task, buttons) {
      const state = task.exists ? task.state : 'Missing';
      container.innerHTML = `
        <div class="eyebrow">Task</div>
        <div class="title-row">
          <strong>${title}</strong>
          ${badge(state)}
        </div>
        <div class="meta">
          ${metaLine('Task name', task.taskName)}
          ${metaLine('Last run', task.lastRunTime)}
          ${metaLine('Next run', task.nextRunTime)}
          ${metaLine('Last result', task.lastTaskResult ?? '')}
        </div>
        <div class="actions">${buttons}</div>
      `;
    }

    function renderZoneUiCard(zoneUi) {
      const running = zoneUi.processes.length > 0;
      zoneUiCard.innerHTML = `
        <div class="eyebrow">Service</div>
        <div class="title-row">
          <strong>Zone Designer</strong>
          ${badge(running ? 'Running' : 'Stopped')}
        </div>
        <div class="meta">
          ${metaLine('URL', zoneUi.url)}
          ${metaLine('Process count', String(zoneUi.processes.length))}
          ${metaLine('Port', String(zoneUi.port))}
        </div>
        <div class="actions">
          <button class="primary" type="button" data-action="zone-ui/start">Start</button>
          <button class="secondary" type="button" data-action="zone-ui/restart">Restart</button>
          <button class="secondary" type="button" data-action="zone-ui/stop">Stop</button>
        </div>
      `;
    }

    function renderHomeAssistantCard(homeAssistant) {
      const unavailable = homeAssistant.unavailableEntityIds || [];
      haCard.innerHTML = `
        <div class="eyebrow">Connectivity</div>
        <div class="title-row">
          <strong>Home Assistant</strong>
          ${badge(homeAssistant.reachable ? 'Reachable' : 'Unreachable')}
        </div>
        <div class="meta">
          ${metaLine('Configured lights', String(homeAssistant.configuredLightCount || 0))}
          ${metaLine('Available lights', String(homeAssistant.availableLightCount || 0))}
          ${metaLine('Unavailable', unavailable.length ? unavailable.join(', ') : 'None')}
          ${homeAssistant.error ? metaLine('Error', homeAssistant.error) : ''}
        </div>
      `;
    }

    function renderConfigCard(config) {
      configCard.innerHTML = `
        <div class="eyebrow">Runtime</div>
        <div class="title-row">
          <strong>Current Configuration</strong>
          ${badge('Live')}
        </div>
        <div class="meta">
          ${metaLine('Dashboard URL', config.dashboardUrl)}
          ${metaLine('Color mode', config.colorSyncMode)}
          ${metaLine('Sample stride', String(config.sampleStride))}
          ${metaLine('Parallel updates', String(config.parallelUpdates))}
          ${metaLine('Light count', String(config.lightCount))}
          ${metaLine('Capture target', config.screenCaptureTarget)}
        </div>
      `;
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const text = await response.text();
      let payload = {};
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = { message: text };
        }
      }
      if (!response.ok) {
        throw new Error(payload.message || response.statusText);
      }
      return payload;
    }

    async function loadStatus() {
      const status = await fetchJson('/api/status');
      dashboardLink.href = status.config.dashboardUrl;
      zoneUiLink.href = status.zoneUi.url;
      renderTaskCard(syncCard, 'Screen Sync', status.syncTask, `
        <button class="primary" type="button" data-action="sync/start">Start</button>
        <button class="secondary" type="button" data-action="sync/restart">Restart</button>
        <button class="secondary" type="button" data-action="sync/stop">Stop</button>
      `);
      renderTaskCard(dashboardCard, 'Dashboard Autostart', status.dashboardTask, '');
      renderZoneUiCard(status.zoneUi);
      renderHomeAssistantCard(status.homeAssistant);
      renderConfigCard(status.config);
      bindActionButtons();
      statusBar.textContent = 'Dashboard refreshed.';
    }

    async function loadLogs() {
      const payload = await fetchJson('/api/logs');
      logOutput.textContent = payload.text;
    }

    function bindActionButtons() {
      document.querySelectorAll('[data-action]').forEach((button) => {
        button.onclick = async () => {
          const action = button.dataset.action;
          statusBar.textContent = `Running ${action}...`;
          try {
            const payload = await fetchJson(`/api/actions/${action}`, { method: 'POST' });
            statusBar.textContent = payload.message || 'Action completed.';
            await loadStatus();
            await loadLogs();
          } catch (error) {
            statusBar.textContent = error.message;
          }
        };
      });
    }

    document.getElementById('refreshLogsButton').addEventListener('click', () => {
      loadLogs().catch((error) => {
        statusBar.textContent = error.message;
      });
    });

    Promise.all([loadStatus(), loadLogs()]).catch((error) => {
      statusBar.textContent = error.message;
    });
    setInterval(() => {
      loadStatus().catch((error) => {
        statusBar.textContent = error.message;
      });
    }, 5000);
    setInterval(() => {
      loadLogs().catch((error) => {
        statusBar.textContent = error.message;
      });
    }, 10000);
  </script>
</body>
</html>
""".replace("__TITLE__", title)
    )


if __name__ == "__main__":
    raise SystemExit(main())