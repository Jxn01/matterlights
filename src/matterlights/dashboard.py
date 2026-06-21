from __future__ import annotations

import base64
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
from matterlights.playback import (
    control_state_from_payload,
    control_state_to_payload,
    load_control_state,
    pattern_cycle_seconds,
    save_control_state,
)


APP = Flask(__name__)
SYNC_TASK_NAME = "MatterLights Screen Sync"
DASHBOARD_TASK_NAME = "MatterLights Dashboard"
ZONE_UI_MODULE = "matterlights.zone_ui"
DASHBOARD_MODULE = "matterlights.dashboard"
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


@APP.post("/api/actions/dashboard/restart")
def restart_dashboard() -> Response:
    settings = load_settings()
    _restart_dashboard_process(settings)
    return jsonify({"ok": True, "message": "Restarting dashboard… this page will reconnect automatically."})


@APP.get("/api/control")
def get_control() -> Response:
    settings = load_settings()
    state = load_control_state(settings.control_state_file)
    payload = control_state_to_payload(state)
    payload["configured"] = settings.control_state_file is not None
    payload["lightCount"] = len(settings.light_entities)
    payload["cycleSeconds"] = round(pattern_cycle_seconds(state.custom.pattern_steps), 3)
    payload["maxPatternTransitionSeconds"] = settings.max_pattern_transition_seconds
    return jsonify(payload)


@APP.post("/api/control")
def set_control() -> tuple[Response, int] | Response:
    settings = load_settings()
    if settings.control_state_file is None:
        return jsonify({"ok": False, "message": "CONTROL_STATE_FILE is not configured"}), 400

    payload = request.get_json(force=True, silent=False)
    try:
        state = control_state_from_payload(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    save_control_state(settings.control_state_file, state)
    return jsonify(
        {
            "ok": True,
            "mode": state.mode,
            "message": f"Applied {state.mode} mode. The sync loop will pick it up automatically.",
        }
    )


def main() -> int:
    settings = load_settings()
    APP.run(host="127.0.0.1", port=settings.dashboard_port, debug=False, use_reloader=False)
    return 0


def _build_dashboard_status(settings: Settings) -> dict[str, Any]:
    control_state = load_control_state(settings.control_state_file)
    return {
        "syncTask": _task_status(SYNC_TASK_NAME),
        "dashboardTask": _task_status(DASHBOARD_TASK_NAME),
        "zoneUi": {
            "url": f"http://127.0.0.1:{settings.zone_ui_port}",
            "port": settings.zone_ui_port,
            "processes": _module_processes(ZONE_UI_MODULE),
        },
        "homeAssistant": _home_assistant_status(settings),
        "playback": {
            "mode": control_state.mode,
            "customType": control_state.custom.type,
            "brightness": control_state.custom.brightness,
            "stepCount": len(control_state.custom.pattern_steps),
            "cycleSeconds": round(pattern_cycle_seconds(control_state.custom.pattern_steps), 3),
            "respectDisplaySleep": settings.respect_display_sleep,
        },
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


def _restart_dashboard_process(settings: Settings) -> None:
    # The dashboard cannot restart itself in-process, so hand the work to a helper
    # that outlives it. The helper waits for the HTTP response to flush, frees the
    # port (whether the dashboard runs as the scheduled task or a manual process),
    # then brings exactly one instance back.
    task = _ps_quote(DASHBOARD_TASK_NAME)
    module = _ps_quote(DASHBOARD_MODULE)
    dashboard_script = _ps_quote(str(REPO_ROOT / "scripts" / "start-dashboard.ps1"))
    script = f"""
Start-Sleep -Seconds 1
$task = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
if ($null -ne $task) {{ Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue }}
Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*-m {module}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Milliseconds 800
if ($null -ne $task) {{
  Start-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
}} else {{
  & '{dashboard_script}' -Port {settings.dashboard_port} -NoBrowser
}}
"""
    _spawn_independent_process(script)


def _spawn_independent_process(script: str) -> None:
    # Create the helper through WMI (Win32_Process.Create) so it runs under the WMI
    # provider host instead of as a child of this process. That keeps it out of the
    # dashboard's scheduled-task job object, so Stop-ScheduledTask — which terminates
    # the whole task tree — cannot take the helper down before it restarts us.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    helper_command = f"powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    launcher = (
        "Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{ CommandLine = '{_ps_quote(helper_command)}' }} | Out-Null"
    )
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launcher],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
    .subtle-sm {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      margin: 0 0 14px;
    }
    .seg-group {
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.12);
      border: 1px solid var(--line);
    }
    .seg {
      border-radius: 999px;
      padding: 9px 18px;
      background: transparent;
      color: var(--muted);
      border: 0;
      box-shadow: none;
    }
    .seg.small { padding: 7px 14px; font-size: 13px; }
    .seg.active {
      color: #08131c;
      background: linear-gradient(135deg, var(--accent), #c4b5fd);
    }
    .custom-panel {
      margin-top: 18px;
      display: grid;
      gap: 18px;
    }
    .custom-panel.hidden { display: none; }
    .editor-block.hidden { display: none; }
    .field-label {
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
    }
    .bright-row input[type="range"] {
      width: 100%;
      accent-color: var(--accent);
    }
    .solid-row {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    input[type="color"] {
      width: 56px;
      height: 40px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: transparent;
      cursor: pointer;
    }
    .big-swatch {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.08), inset 0 0 10px rgba(0, 0, 0, 0.35);
      display: inline-block;
    }
    .pattern-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 10px;
    }
    .timeline {
      position: relative;
      height: 34px;
      border-radius: 12px;
      border: 1px solid var(--line);
      overflow: hidden;
      background: #02050a;
    }
    .playhead {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 2px;
      background: rgba(255, 255, 255, 0.9);
      box-shadow: 0 0 8px rgba(255, 255, 255, 0.7);
      transform: translateX(-1px);
    }
    .preview-row {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 13px;
    }
    .step-list {
      display: grid;
      gap: 10px;
    }
    .step {
      display: grid;
      grid-template-columns: 168px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      padding: 12px;
      background: rgba(15, 23, 42, 0.55);
      border: 1px solid var(--line);
      border-radius: 14px;
    }
    .step-fields {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }
    .step-fields label {
      display: grid;
      gap: 4px;
      font-size: 12px;
      color: var(--muted);
    }
    .step-fields input {
      width: 92px;
      padding: 8px 10px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: rgba(2, 6, 23, 0.6);
      color: var(--text);
      font: inherit;
    }
    .step-buttons {
      display: flex;
      gap: 6px;
    }
    .step-buttons button {
      width: 34px;
      height: 34px;
      padding: 0;
      border-radius: 10px;
      background: rgba(148, 163, 184, 0.16);
      color: var(--text);
      border: 1px solid var(--line);
      box-shadow: none;
      font-size: 15px;
      line-height: 1;
    }
    .step-buttons button:disabled { opacity: 0.35; cursor: default; }
    .muted { color: var(--muted); }
    .seg.tiny { padding: 5px 10px; font-size: 12px; }
    .seg-group.tiny { padding: 3px; }
    .editor-block.white-row { display: grid; gap: 10px; }
    .kelvin-track {
      height: 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: linear-gradient(90deg, #ff8b1e, #ffd6a3, #ffffff, #cfe0ff, #a9c6ff);
    }
    input[type="range"].kelvin-slider { width: 100%; accent-color: #cfe0ff; margin-top: -2px; }
    .step-color-cell { display: grid; gap: 8px; }
    .step-white-control { display: flex; align-items: center; gap: 8px; }
    .step-white-control input[type="range"] { flex: 1; accent-color: #cfe0ff; }
    .step-white-control .step-kelvin-val { font-size: 12px; min-width: 44px; }
    .hidden { display: none; }
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
      <article id="customCard" class="card full">
        <div class="eyebrow">Playback</div>
        <div class="title-row">
          <strong>Playback Mode</strong>
          <span id="playbackBadge"></span>
        </div>
        <p class="subtle-sm">Autonomous follows your screen colors. Custom ignores the screen and drives every light from a static color or a looping pattern.</p>
        <div class="seg-group" role="tablist">
          <button id="modeAutonomous" class="seg" type="button">Autonomous</button>
          <button id="modeCustom" class="seg" type="button">Custom</button>
        </div>
        <div id="customPanel" class="custom-panel">
          <div class="seg-group">
            <button id="typeSolid" class="seg small" type="button">Static color</button>
            <button id="typePattern" class="seg small" type="button">Pattern</button>
          </div>
          <div class="bright-row">
            <label class="field-label" for="brightness">Brightness <span id="brightnessValue" class="mono"></span></label>
            <input id="brightness" type="range" min="1" max="255" />
          </div>
          <div id="solidEditor" class="editor-block">
            <div class="seg-group">
              <button id="solidModeColor" class="seg small" type="button">Color</button>
              <button id="solidModeWhite" class="seg small" type="button">White</button>
            </div>
            <div id="solidColorRow" class="solid-row">
              <input id="solidColor" type="color" />
              <span id="solidSwatch" class="big-swatch"></span>
            </div>
            <div id="solidWhiteRow" class="editor-block white-row">
              <label class="field-label" for="solidKelvin">Color temperature <span id="solidKelvinValue" class="mono"></span></label>
              <div class="kelvin-track"></div>
              <input id="solidKelvin" type="range" min="2200" max="6500" step="50" class="kelvin-slider" />
              <div class="preview-row">
                <span id="solidWhiteSwatch" class="big-swatch"></span>
                <span class="muted">Warm ↔ cool white</span>
              </div>
            </div>
          </div>
          <div id="patternEditor" class="editor-block">
            <div class="pattern-head">
              <span>Colors in the loop (each holds, then the next fades in)</span>
              <span id="cycleLabel" class="mono"></span>
            </div>
            <div id="fadeNote" class="subtle-sm"></div>
            <div id="timeline" class="timeline">
              <div id="timelinePlayhead" class="playhead"></div>
            </div>
            <div class="preview-row">
              <span id="livePreview" class="big-swatch"></span>
              <span class="muted">Live preview of the loop, updated in real time</span>
            </div>
            <div id="stepList" class="step-list"></div>
            <button id="addStep" class="secondary" type="button">+ Add color</button>
          </div>
        </div>
        <div class="actions">
          <button id="applyControl" class="primary" type="button">Apply</button>
          <span id="controlStatus" class="muted"></span>
        </div>
      </article>
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

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[ch]));
    }

    function metaLine(label, value) {
      return `<div><strong>${label}:</strong> <span class="mono">${escapeHtml(value || '—')}</span></div>`;
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
      renderTaskCard(dashboardCard, 'Dashboard Autostart', status.dashboardTask, `
        <button class="primary dashboard-restart-button" type="button">Restart Dashboard</button>
      `);
      renderZoneUiCard(status.zoneUi);
      renderHomeAssistantCard(status.homeAssistant);
      renderConfigCard(status.config);
      renderPlaybackBadge(status.playback);
      bindActionButtons();
      statusBar.textContent = 'Dashboard refreshed.';
    }

    function renderPlaybackBadge(playback) {
      if (!playback) return;
      let label = 'Autonomous';
      if (playback.mode === 'custom') {
        label = playback.customType === 'pattern'
          ? `Custom · Pattern (${playback.cycleSeconds}s loop)`
          : 'Custom · Static color';
      }
      const sleepNote = playback.respectDisplaySleep ? '' : ' · ignores screen sleep';
      playbackBadge.innerHTML = `<span class="badge ${playback.mode === 'custom' ? 'good' : 'warn'}">${label}${sleepNote}</span>`;
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
      const restartButton = document.querySelector('.dashboard-restart-button');
      if (restartButton) restartButton.onclick = restartDashboard;
    }

    async function restartDashboard() {
      statusBar.textContent = 'Restarting dashboard…';
      try {
        await fetchJson('/api/actions/dashboard/restart', { method: 'POST' });
      } catch (error) {
        // The server drops the connection as it restarts; that is expected.
      }
      waitForDashboard(0);
    }

    function waitForDashboard(attempt) {
      if (attempt > 30) {
        statusBar.textContent = 'Dashboard did not come back automatically. Reload the page manually.';
        return;
      }
      statusBar.textContent = `Dashboard restarting… reconnecting (${attempt + 1}).`;
      setTimeout(async () => {
        try {
          await fetchJson('/api/status');
          statusBar.textContent = 'Dashboard restarted. Reloading…';
          location.reload();
        } catch (error) {
          waitForDashboard(attempt + 1);
        }
      }, 1000);
    }

    // ---- Playback mode editor ----
    const playbackBadge = document.getElementById('playbackBadge');
    const customPanel = document.getElementById('customPanel');
    const modeAutonomousButton = document.getElementById('modeAutonomous');
    const modeCustomButton = document.getElementById('modeCustom');
    const typeSolidButton = document.getElementById('typeSolid');
    const typePatternButton = document.getElementById('typePattern');
    const brightnessInput = document.getElementById('brightness');
    const brightnessValue = document.getElementById('brightnessValue');
    const solidEditor = document.getElementById('solidEditor');
    const patternEditor = document.getElementById('patternEditor');
    const solidColorInput = document.getElementById('solidColor');
    const solidSwatch = document.getElementById('solidSwatch');
    const solidModeColorButton = document.getElementById('solidModeColor');
    const solidModeWhiteButton = document.getElementById('solidModeWhite');
    const solidColorRow = document.getElementById('solidColorRow');
    const solidWhiteRow = document.getElementById('solidWhiteRow');
    const solidKelvinInput = document.getElementById('solidKelvin');
    const solidKelvinValue = document.getElementById('solidKelvinValue');
    const solidWhiteSwatch = document.getElementById('solidWhiteSwatch');
    const stepList = document.getElementById('stepList');
    const addStepButton = document.getElementById('addStep');
    const cycleLabel = document.getElementById('cycleLabel');
    const fadeNote = document.getElementById('fadeNote');
    const timeline = document.getElementById('timeline');
    const timelinePlayhead = document.getElementById('timelinePlayhead');
    const livePreview = document.getElementById('livePreview');
    const applyControlButton = document.getElementById('applyControl');
    const controlStatus = document.getElementById('controlStatus');

    let control = null;
    let fadeCap = 0;
    let previewStart = (typeof performance !== 'undefined' ? performance.now() : 0);

    function renderFadeNote() {
      if (fadeCap > 0) {
        fadeNote.textContent = `Fades are capped at ${fadeCap}s. Note: many Matter bulbs freeze on fade commands — if yours lock up, set MAX_PATTERN_TRANSITION_SECONDS=0 in .env.`;
      } else {
        fadeNote.textContent = 'Fades are off (colors snap). Many Matter bulbs freeze on fade commands, so the per-color "Fade in" is ignored until you set MAX_PATTERN_TRANSITION_SECONDS in .env above 0.';
      }
    }

    function clampInt(value, min, max) {
      if (Number.isNaN(value)) return min;
      return Math.min(max, Math.max(min, Math.round(value)));
    }

    function toHex(rgb) {
      return '#' + rgb.map((c) => clampInt(c, 0, 255).toString(16).padStart(2, '0')).join('');
    }

    function fromHex(hex) {
      const n = hex.replace('#', '');
      return [
        parseInt(n.slice(0, 2), 16) || 0,
        parseInt(n.slice(2, 4), 16) || 0,
        parseInt(n.slice(4, 6), 16) || 0,
      ];
    }

    function scaledHex(rgb, brightness) {
      const scale = clampInt(brightness, 1, 255) / 255;
      return toHex(rgb.map((c) => c * scale));
    }

    function kelvinToRgb(kelvin) {
      const t = Math.max(1000, Math.min(40000, kelvin)) / 100;
      let r;
      let g;
      let b;
      if (t <= 66) { r = 255; } else { r = 329.698727446 * Math.pow(t - 60, -0.1332047592); }
      if (t <= 66) { g = 99.4708025861 * Math.log(t) - 161.1195681661; }
      else { g = 288.1221695283 * Math.pow(t - 60, -0.0755148492); }
      if (t >= 66) { b = 255; } else if (t <= 19) { b = 0; }
      else { b = 138.5177312231 * Math.log(t - 10) - 305.0447927307; }
      return [clampInt(r, 0, 255), clampInt(g, 0, 255), clampInt(b, 0, 255)];
    }

    function displayRgb(item) {
      return item.mode === 'white' ? kelvinToRgb(item.kelvin) : item.color;
    }

    function patternSteps() {
      return control.custom.pattern.steps;
    }

    function cycleSeconds(steps) {
      return steps.reduce((sum, s) => sum + Math.max(0, s.hold) + Math.max(0, s.transition), 0);
    }

    function lerp(a, b, f) {
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
    }

    function colorAt(steps, t) {
      const n = steps.length;
      if (n === 0) return [0, 0, 0];
      if (n === 1) return displayRgb(steps[0]);
      const cycle = cycleSeconds(steps);
      if (cycle <= 0) return displayRgb(steps[0]);
      const time = ((t % cycle) + cycle) % cycle;
      let cursor = 0;
      for (let i = 0; i < n; i += 1) {
        const trans = Math.max(0, steps[i].transition);
        const hold = Math.max(0, steps[i].hold);
        const cur = displayRgb(steps[i]);
        const prev = displayRgb(steps[(i - 1 + n) % n]);
        if (time < cursor + trans) {
          const f = trans > 0 ? (time - cursor) / trans : 1;
          return lerp(prev, cur, f);
        }
        if (time < cursor + trans + hold) {
          return cur;
        }
        cursor += trans + hold;
      }
      return displayRgb(steps[n - 1]);
    }

    function timelineGradient(steps) {
      const n = steps.length;
      const cycle = cycleSeconds(steps);
      if (n === 0) return '#02050a';
      if (n === 1 || cycle <= 0) return toHex(displayRgb(steps[0]));
      const stops = [];
      let cursor = 0;
      for (let i = 0; i < n; i += 1) {
        const trans = Math.max(0, steps[i].transition);
        const hold = Math.max(0, steps[i].hold);
        const cur = displayRgb(steps[i]);
        const prev = displayRgb(steps[(i - 1 + n) % n]);
        if (trans > 0) {
          stops.push(`${toHex(prev)} ${(cursor / cycle) * 100}%`);
          stops.push(`${toHex(cur)} ${((cursor + trans) / cycle) * 100}%`);
        } else {
          stops.push(`${toHex(cur)} ${(cursor / cycle) * 100}%`);
        }
        stops.push(`${toHex(cur)} ${((cursor + trans + hold) / cycle) * 100}%`);
        cursor += trans + hold;
      }
      return `linear-gradient(90deg, ${stops.join(', ')})`;
    }

    function setActive(button, active) {
      button.classList.toggle('active', active);
    }

    function sanitizeSeconds(value) {
      const parsed = parseFloat(value);
      if (Number.isNaN(parsed) || parsed < 0) return 0;
      return Math.min(3600, parsed);
    }

    function moveStep(index, delta) {
      const steps = patternSteps();
      const target = index + delta;
      if (target < 0 || target >= steps.length) return;
      const [moved] = steps.splice(index, 1);
      steps.splice(target, 0, moved);
      renderSteps();
      renderDerived();
    }

    function renderSteps() {
      const steps = patternSteps();
      stepList.innerHTML = '';
      steps.forEach((step, index) => {
        const isWhite = step.mode === 'white';
        const row = document.createElement('div');
        row.className = 'step';
        row.innerHTML = `
          <div class="step-color-cell">
            <div class="seg-group tiny">
              <button type="button" class="seg tiny step-mode-rgb">RGB</button>
              <button type="button" class="seg tiny step-mode-white">White</button>
            </div>
            <input type="color" class="step-color" value="${toHex(step.color)}">
            <div class="step-white-control">
              <input type="range" class="step-kelvin" min="2200" max="6500" step="50" value="${step.kelvin}">
              <span class="step-kelvin-val mono">${step.kelvin}K</span>
            </div>
          </div>
          <div class="step-fields">
            <label>Hold (s)<input type="number" class="step-hold" min="0" step="0.5" value="${step.hold}"></label>
            <label>Fade in (s)<input type="number" class="step-transition" min="0" step="0.5" value="${step.transition}"></label>
          </div>
          <div class="step-buttons">
            <button type="button" class="step-up" title="Move up">↑</button>
            <button type="button" class="step-down" title="Move down">↓</button>
            <button type="button" class="step-remove" title="Remove">✕</button>
          </div>
        `;
        const rgbButton = row.querySelector('.step-mode-rgb');
        const whiteButton = row.querySelector('.step-mode-white');
        const colorInput = row.querySelector('.step-color');
        const whiteControl = row.querySelector('.step-white-control');
        const kelvinInput = row.querySelector('.step-kelvin');
        const kelvinVal = row.querySelector('.step-kelvin-val');
        rgbButton.classList.toggle('active', !isWhite);
        whiteButton.classList.toggle('active', isWhite);
        colorInput.classList.toggle('hidden', isWhite);
        whiteControl.classList.toggle('hidden', !isWhite);
        rgbButton.addEventListener('click', () => { step.mode = 'rgb'; renderSteps(); renderDerived(); });
        whiteButton.addEventListener('click', () => { step.mode = 'white'; renderSteps(); renderDerived(); });
        colorInput.addEventListener('input', () => { step.color = fromHex(colorInput.value); renderDerived(); });
        kelvinInput.addEventListener('input', () => {
          step.kelvin = clampInt(parseInt(kelvinInput.value, 10), 2200, 6500);
          kelvinVal.textContent = `${step.kelvin}K`;
          renderDerived();
        });
        const holdInput = row.querySelector('.step-hold');
        holdInput.addEventListener('input', () => { step.hold = sanitizeSeconds(holdInput.value); renderDerived(); });
        const transInput = row.querySelector('.step-transition');
        transInput.addEventListener('input', () => { step.transition = sanitizeSeconds(transInput.value); renderDerived(); });
        const upButton = row.querySelector('.step-up');
        upButton.disabled = index === 0;
        upButton.addEventListener('click', () => moveStep(index, -1));
        const downButton = row.querySelector('.step-down');
        downButton.disabled = index === steps.length - 1;
        downButton.addEventListener('click', () => moveStep(index, 1));
        const removeButton = row.querySelector('.step-remove');
        removeButton.disabled = steps.length <= 1;
        removeButton.addEventListener('click', () => { steps.splice(index, 1); renderSteps(); renderDerived(); });
        stepList.append(row);
      });
    }

    function renderDerived() {
      if (!control) return;
      const steps = patternSteps();
      const cycle = cycleSeconds(steps);
      cycleLabel.textContent = `Loop length: ${cycle.toFixed(1)}s · ${steps.length}/24 colors`;
      timeline.style.background = timelineGradient(steps);
      solidSwatch.style.background = scaledHex(control.custom.solid.color, control.custom.brightness);
      solidWhiteSwatch.style.background = scaledHex(kelvinToRgb(control.custom.solid.kelvin), control.custom.brightness);
      addStepButton.disabled = control.custom.type === 'pattern' && steps.length >= 24;
    }

    function renderControlEditor() {
      if (!control) return;
      const isCustom = control.mode === 'custom';
      setActive(modeAutonomousButton, !isCustom);
      setActive(modeCustomButton, isCustom);
      customPanel.classList.toggle('hidden', !isCustom);

      const isPattern = control.custom.type === 'pattern';
      setActive(typeSolidButton, !isPattern);
      setActive(typePatternButton, isPattern);
      solidEditor.classList.toggle('hidden', isPattern);
      patternEditor.classList.toggle('hidden', !isPattern);

      brightnessInput.value = String(control.custom.brightness);
      brightnessValue.textContent = String(control.custom.brightness);

      const solidWhite = control.custom.solid.mode === 'white';
      setActive(solidModeColorButton, !solidWhite);
      setActive(solidModeWhiteButton, solidWhite);
      solidColorRow.classList.toggle('hidden', solidWhite);
      solidWhiteRow.classList.toggle('hidden', !solidWhite);
      solidColorInput.value = toHex(control.custom.solid.color);
      solidKelvinInput.value = String(control.custom.solid.kelvin);
      solidKelvinValue.textContent = `${control.custom.solid.kelvin}K`;

      renderFadeNote();
      renderSteps();
      renderDerived();
    }

    modeAutonomousButton.addEventListener('click', () => { control.mode = 'autonomous'; renderControlEditor(); });
    modeCustomButton.addEventListener('click', () => { control.mode = 'custom'; renderControlEditor(); });
    typeSolidButton.addEventListener('click', () => { control.custom.type = 'solid'; renderControlEditor(); });
    typePatternButton.addEventListener('click', () => { control.custom.type = 'pattern'; renderControlEditor(); });
    brightnessInput.addEventListener('input', () => {
      control.custom.brightness = clampInt(parseInt(brightnessInput.value, 10), 1, 255);
      brightnessValue.textContent = String(control.custom.brightness);
      renderDerived();
    });
    solidColorInput.addEventListener('input', () => {
      control.custom.solid.color = fromHex(solidColorInput.value);
      renderDerived();
    });
    solidModeColorButton.addEventListener('click', () => { control.custom.solid.mode = 'rgb'; renderControlEditor(); });
    solidModeWhiteButton.addEventListener('click', () => { control.custom.solid.mode = 'white'; renderControlEditor(); });
    solidKelvinInput.addEventListener('input', () => {
      control.custom.solid.kelvin = clampInt(parseInt(solidKelvinInput.value, 10), 2200, 6500);
      solidKelvinValue.textContent = `${control.custom.solid.kelvin}K`;
      renderDerived();
    });
    addStepButton.addEventListener('click', () => {
      patternSteps().push({ color: [120, 200, 255], hold: 3, transition: 1, mode: 'rgb', kelvin: 2700 });
      renderSteps();
      renderDerived();
    });
    applyControlButton.addEventListener('click', applyControl);

    setInterval(() => {
      if (!control || control.mode !== 'custom' || control.custom.type !== 'pattern') return;
      const steps = patternSteps();
      const cycle = cycleSeconds(steps);
      const t = ((typeof performance !== 'undefined' ? performance.now() : Date.now()) - previewStart) / 1000;
      livePreview.style.background = scaledHex(colorAt(steps, t), control.custom.brightness);
      if (cycle > 0) {
        timelinePlayhead.style.display = 'block';
        timelinePlayhead.style.left = `${((((t % cycle) + cycle) % cycle) / cycle) * 100}%`;
      } else {
        timelinePlayhead.style.display = 'none';
      }
    }, 80);

    async function loadControl() {
      const payload = await fetchJson('/api/control');
      const custom = payload.custom || {};
      const solid = custom.solid || {};
      const pattern = custom.pattern || {};
      control = {
        mode: payload.mode || 'autonomous',
        custom: {
          type: custom.type || 'solid',
          brightness: custom.brightness || 255,
          solid: {
            mode: solid.mode === 'white' ? 'white' : 'rgb',
            color: solid.color || [255, 255, 255],
            kelvin: typeof solid.kelvin === 'number' ? solid.kelvin : 2700,
          },
          pattern: {
            steps: (pattern.steps || []).map((s) => ({
              color: s.color || [255, 255, 255],
              hold: typeof s.hold === 'number' ? s.hold : 0,
              transition: typeof s.transition === 'number' ? s.transition : 0,
              mode: s.mode === 'white' ? 'white' : 'rgb',
              kelvin: typeof s.kelvin === 'number' ? s.kelvin : 2700,
            })),
          },
        },
      };
      if (control.custom.pattern.steps.length === 0) {
        control.custom.pattern.steps = [{ color: [255, 0, 0], hold: 3, transition: 1, mode: 'rgb', kelvin: 2700 }];
      }
      fadeCap = typeof payload.maxPatternTransitionSeconds === 'number' ? payload.maxPatternTransitionSeconds : 0;
      previewStart = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      renderControlEditor();
      if (payload.configured === false) {
        applyControlButton.disabled = true;
        controlStatus.textContent = 'CONTROL_STATE_FILE is not configured; changes cannot be saved.';
      }
    }

    async function applyControl() {
      controlStatus.textContent = 'Applying…';
      const body = {
        mode: control.mode,
        custom: {
          type: control.custom.type,
          brightness: control.custom.brightness,
          solid: {
            mode: control.custom.solid.mode,
            color: control.custom.solid.color,
            kelvin: control.custom.solid.kelvin,
          },
          pattern: {
            steps: patternSteps().map((s) => ({
              mode: s.mode,
              color: s.color,
              kelvin: s.kelvin,
              hold: s.hold,
              transition: s.transition,
            })),
          },
        },
      };
      try {
        const payload = await fetchJson('/api/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        controlStatus.textContent = payload.message || 'Applied.';
        await loadStatus();
      } catch (error) {
        controlStatus.textContent = error.message;
      }
    }

    document.getElementById('refreshLogsButton').addEventListener('click', () => {
      loadLogs().catch((error) => {
        statusBar.textContent = error.message;
      });
    });

    Promise.all([loadStatus(), loadLogs(), loadControl()]).catch((error) => {
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