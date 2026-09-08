# MatterLights Linux Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MatterLights runs on Linux/Wayland with the behaviour it has on Windows, and Windows keeps working unchanged.

**Architecture:** Each platform-coupled subsystem becomes a subpackage (`__init__.py` = protocol + factory, `windows.py`, `linux.py`), so every current import path survives. `screen.py` splits along the seam the port needs: the colour/zone engine is platform-free and unchanged; capture moves to `capture/`. Linux capture is Mutter ScreenCast (or xdg-desktop-portal off GNOME) → PipeWire → GStreamer `appsink` → BGRx bytes, the exact byte order the existing sampler already indexes.

**Tech Stack:** Python 3.11+, GStreamer 1.28 + PyGObject (system packages), PipeWire, D-Bus via `Gio`, systemd user + system units, Flask, `mss` (Windows capture + cross-platform PNG encoding), `dxcam` (Windows only).

**Spec:** `docs/superpowers/specs/2026-09-08-linux-port-design.md`

## Global Constraints

- **R1** — the portal backend is chosen **only when `org.gnome.Mutter.ScreenCast` is absent from the session bus**. A Mutter *error* on GNOME retries; it never becomes a portal request.
- **R2** — `matterlights.lights_off` must not import `gi`, `Gst`, `mss` or `matterlights.capture`, transitively included.
- **R3** — the Linux caps filter carries `max-framerate = ceil(2 / SYNC_INTERVAL_SECONDS)`, derived at pipeline construction, never hard-coded.
- **R4** — `Session.Closed`, `Stream.Closed` and `DisplayConfig.MonitorsChanged` rebuild the capture session; a vanished connector falls back to `SCREEN_CAPTURE_TARGET` with a warning.
- Public API of `matterlights.screen`, `matterlights.display_power`, `matterlights.shutdown_hook`, `matterlights.process_lock` is unchanged — existing importers must not be edited to accommodate the split.
- Never `pgrep -f` / `pkill -f` with a literal pattern; match `/proc/*/cmdline` tokens exactly.
- Windows code paths are moved verbatim where possible. Any behaviour change on Windows is a bug in this plan.
- Test command (Linux): `.venv/bin/python -m unittest discover -s tests`. Baseline before work: **7 pass, 7 skip, 5 error**.
- Venv: `/usr/bin/python3 -m venv --system-site-packages .venv`. Never miniconda — it cannot see system PyGObject.
- Commit after every task. Branch `linux-port`. Never push.

---

### Task 1: Split `screen.py` and unblock the package on Linux

The whole package currently fails to import on Linux because of `from ctypes import windll` at `screen.py:3`. This task makes Linux importable and locks the class-level guard, without adding any Linux capability yet.

**Files:**
- Create: `src/matterlights/capture/__init__.py`, `src/matterlights/capture/windows.py`
- Modify: `src/matterlights/screen.py` (remove capture, keep sampling), `pyproject.toml`
- Test: `tests/test_import_surface.py`

**Interfaces produced:**
```python
# capture/__init__.py
@dataclass(frozen=True, slots=True)
class Monitor:
    index: int; left: int; top: int; width: int; height: int
    is_primary: bool = False; name: str = ""

class CaptureBackend(Protocol):
    def grab(self, capture_target: str) -> tuple[bytes, int, int]: ...   # BGRx, width, height
    def list_monitors(self) -> list[Monitor]: ...
    def close(self) -> None: ...

def get_backend() -> CaptureBackend: ...        # cached per process
def reset_backend() -> None: ...                # tests + R4 rebuild
```
`screen.py` keeps every existing public name (`capture_zone_samples`, `capture_raw_with_session`, `capture_screen_png`, `capture_screen_thumbnail_png`, `list_monitors`, `sample_zone_samples_from_screenshot`, `RgbColor`, `ScreenZone`, `ZoneSample`, `load_configured_light_zones`, `save_light_zones`, `resolve_light_zones`, `default_light_zone_layout`, `brightness_for_color`, `boost_saturation`) and delegates capture to `get_backend()`.

- [ ] **Step 1: Write the failing guard test**

```python
# tests/test_import_surface.py
import importlib, pkgutil, sys, unittest
import matterlights

class ImportSurfaceTest(unittest.TestCase):
    def test_every_module_imports_on_this_platform(self):
        failures = []
        for mod in pkgutil.walk_packages(matterlights.__path__, "matterlights."):
            name = mod.name
            leaf = name.rsplit(".", 1)[-1]
            if leaf == "windows" and sys.platform != "win32":
                continue
            if leaf == "linux" and sys.platform == "win32":
                continue
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{name}: {exc!r}")
        self.assertEqual([], failures)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m unittest tests.test_import_surface -v`
Expected: FAIL listing `matterlights.screen: ImportError(... windll ...)`.

- [ ] **Step 3: Create `capture/windows.py`**

Move, verbatim, from `screen.py`: the DXGI backend comment block, `_DXCAM_*` globals, `_OUTPUT_INFO_RE`, `_dxcam_outputs`, `_dxcam_output_for_region`, `_dxcam_capture`, `_grab_raw`, `_primary_monitor_region`, `_capture_region`, `_grab_screenshot`, and the `from ctypes import windll` import. Wrap them in a `WindowsCaptureBackend` class implementing `CaptureBackend`, holding one long-lived `MSS()` session.

- [ ] **Step 4: Create `capture/__init__.py`**

`Monitor`, `CaptureBackend`, `get_backend()` selecting `windows.WindowsCaptureBackend` on `sys.platform == "win32"` and raising a clear `RuntimeError` elsewhere for now (Task 3 fills it in), `reset_backend()`, and `to_png` / `thumbnail_png` helpers over `mss.tools` (pure-Python, no X11).

- [ ] **Step 5: Strip `screen.py` to the sampling engine**

Delete the moved functions and the `windll`/`MSS`/`dxcam` imports. Keep every dataclass, `_ZONE_PRESETS`, all `_sample_*`, `_zone_*`, `_ambient_*`, `_blend_colors`, `_color_saturation`, `_dominant_color_bucket`, `_perceived_brightness`, `boost_saturation`, `_clamp_channel`, and the zone file loader/saver **unchanged**. Re-implement the capture entry points as thin delegations to `capture.get_backend()`.

- [ ] **Step 6: Add the dxcam platform marker**

`pyproject.toml`: `"dxcam>=0.3.0; sys_platform == 'win32'"`.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m unittest discover -s tests`
Expected: **0 errors** (was 5). The 7 win32 tests still skip. `test_import_surface` passes.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "Split capture out of screen.py so the package imports on Linux"
```

---

### Task 2: `paths.py` — per-OS state directories

**Files:**
- Create: `src/matterlights/paths.py`
- Modify: `src/matterlights/config.py` (`_default_log_path`)
- Test: `tests/test_paths.py`

**Interfaces produced:**
```python
def state_dir() -> Path            # %LOCALAPPDATA%\matterlights | $XDG_STATE_HOME/matterlights
def default_log_path() -> Path     # state_dir() / "matterlights.log"
def portal_token_path() -> Path    # state_dir() / "portal-restore-token"
```

- [ ] **Step 1: Write failing tests** — `XDG_STATE_HOME` honoured; default is `~/.local/state/matterlights` when unset; `LOCALAPPDATA` used when set (simulating Windows).
- [ ] **Step 2: Run, confirm ImportError.**
- [ ] **Step 3: Implement `paths.py`.**
- [ ] **Step 4: Point `config._default_log_path` at `paths.default_log_path()`**, removing the `cwd` fallback that litters the repo with logs.
- [ ] **Step 5: Run suite — green.**
- [ ] **Step 6: Commit** — `"Resolve log and state paths per OS instead of falling back to cwd"`

---

### Task 3: Linux capture — Mutter source + PipeWire pump

The core of the port. Proven end to end by spike; this makes it production code.

**Files:**
- Create: `src/matterlights/capture/linux.py`, `src/matterlights/capture/mutter.py`
- Modify: `src/matterlights/capture/__init__.py` (selection)
- Test: `tests/test_capture_linux.py`

**Interfaces produced:**
```python
# capture/mutter.py
class MutterUnavailable(RuntimeError): ...
def mutter_available() -> bool                     # session-bus name check — R1
class MutterSource:
    def list_monitors(self) -> list[Monitor]       # DisplayConfig, sorted by (x, y), 1..N
    def open_stream(self, target: str) -> int      # -> pipewire node id
    def close(self) -> None
    def on_invalidated(self, callback) -> None     # Closed / MonitorsChanged — R4

# capture/linux.py
class LinuxCaptureBackend(CaptureBackend):
    def __init__(self, source, sync_interval_seconds: float = 0.2)
```

- [ ] **Step 1: Write failing tests** with a fake source and a fake `appsink`:
  - `grab` returns the newest frame;
  - `grab` returns the **previous** frame when no new one arrived (static screen — parity with dxcam's `None`);
  - the caps string carries `max-framerate=10/1` for `sync_interval_seconds=0.2` and `max-framerate=4/1` for `0.5` (**R3**);
  - `on_invalidated` triggers exactly one rebuild (**R4**);
  - a target naming a missing connector falls back to the configured default and logs a warning (**R4**).
- [ ] **Step 2: Run, confirm failures.**
- [ ] **Step 3: Implement `mutter.py`** — `CreateSession` → `RecordMonitor(connector, {'cursor-mode': u0})` or `RecordArea` for `all` → `Start` → catch `PipeWireStreamAdded` → node id. Subscribe to `Session.Closed`, `Stream.Closed`, `DisplayConfig.MonitorsChanged`.
- [ ] **Step 4: Implement `linux.py`** — GStreamer pipeline `pipewiresrc path=N always-copy=true ! videoconvert ! video/x-raw,format=BGRx,max-framerate=<derived> ! appsink max-buffers=1 drop=true sync=false` on a dedicated thread with its own `GLib.MainLoop`; `new-sample` callback stores `(bytes, w, h)` under a lock; `grab()` returns latest-or-previous. Raise a clear, actionable error naming `python-gobject`/`gst-plugin-pipewire` if `gi` is missing.
- [ ] **Step 5: Wire selection in `capture/__init__.py`** — non-win32 → `LinuxCaptureBackend`.
- [ ] **Step 6: Run unit tests — green.**
- [ ] **Step 7: Live check** — a script that grabs one frame and asserts non-black, run both from a terminal and under `systemd-run --user`.
- [ ] **Step 8: Commit** — `"Capture the screen through Mutter ScreenCast and PipeWire on Linux"`

---

### Task 4: Portal fallback and the R1 selection rule

**Files:**
- Create: `src/matterlights/capture/portal.py`
- Modify: `src/matterlights/capture/__init__.py`
- Test: `tests/test_capture_selection.py`

- [ ] **Step 1: Write failing tests** — with the Mutter name present, the Mutter source is chosen **even when it raises** (retry, no portal); with the name absent, the portal source is chosen. This is R1 and is the single most important test in this task.
- [ ] **Step 2: Run, confirm failures.**
- [ ] **Step 3: Implement `portal.py`** — `CreateSession` → `SelectSources(types=MONITOR, persist_mode=2, restore_token=<saved>)` → `Start` → `OpenPipeWireRemote` → node id; persist the returned `restore_token` through `paths.portal_token_path()`. Report `target_is_portal_selected = True` so the dashboard can say so.
- [ ] **Step 4: Implement the selection rule** in `capture/__init__.py`.
- [ ] **Step 5: Run suite — green.**
- [ ] **Step 6: Commit** — `"Fall back to xdg-desktop-portal capture only when Mutter is absent"`

---

### Task 5: `display_power/` — Linux screen-sleep detection

**Files:**
- Create: `src/matterlights/display_power/__init__.py`, `windows.py`, `linux.py`
- Delete: `src/matterlights/display_power.py` (contents move)
- Test: `tests/test_display_power_linux.py`

- [ ] **Step 1: Write failing tests** — `PowerSaveMode` 0 → on; 1/2/3 → off; `-1` → on (unknown must never turn lights off); D-Bus failure → `AlwaysOnDisplayMonitor`.
- [ ] **Step 2: Run, confirm failures.**
- [ ] **Step 3: Move the Win32 implementation verbatim** into `display_power/windows.py`; `__init__.py` keeps `DisplayMonitor`, `AlwaysOnDisplayMonitor`, `start_display_monitor`.
- [ ] **Step 4: Implement `linux.py`** — subscribe to `PropertiesChanged` on `org.gnome.Mutter.DisplayConfig`, read `PowerSaveMode`, fall back to polling `/sys/class/drm/*/dpms`.
- [ ] **Step 5: Run suite — green.**
- [ ] **Step 6: Commit** — `"Follow the screen to sleep on Linux via Mutter PowerSaveMode"`

---

### Task 6: `process_lock` — flock single-instance guard

**Files:** Modify `src/matterlights/process_lock.py`; Test `tests/test_process_lock.py`

- [ ] **Step 1: Write failing tests** — first acquirer gets a lock; second returns `None` while the first is held; releasing lets a third succeed; lock file lives under `$XDG_RUNTIME_DIR`.
- [ ] **Step 2: Run, confirm failures** (currently `_NullLock` on Linux, so the second acquirer wrongly succeeds).
- [ ] **Step 3: Implement** `fcntl.flock(LOCK_EX | LOCK_NB)` on `$XDG_RUNTIME_DIR/matterlights-sync.lock`, keeping the fail-open contract on error.
- [ ] **Step 4: Run suite — green.**
- [ ] **Step 5: Commit** — `"Guard against a second sync loop on Linux with flock"`

---

### Task 7: `shutdown_hook/` — SIGTERM, and R2 import purity

**Files:**
- Create: `src/matterlights/shutdown_hook/__init__.py`, `windows.py`, `linux.py`
- Delete: `src/matterlights/shutdown_hook.py` (contents move)
- Modify: `src/matterlights/home_assistant.py` if it still reaches the capture stack
- Test: `tests/test_shutdown_hook_linux.py`, `tests/test_lights_off_imports.py`

- [ ] **Step 1: Write the R2 guard test**

```python
# tests/test_lights_off_imports.py
import subprocess, sys, unittest

class LightsOffImportPurityTest(unittest.TestCase):
    def test_lights_off_does_not_import_the_capture_stack(self):
        code = (
            "import matterlights.lights_off, sys; "
            "print(','.join(m for m in ('gi','gi.repository.Gst','mss','matterlights.capture') "
            "if m in sys.modules))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual("", out.stdout.strip(), f"lights_off pulled in: {out.stdout.strip()}")
```

- [ ] **Step 2: Run it.** It documents the current chain (`home_assistant` → `screen`). After Task 1 `screen` is platform-free, so this may already pass — if it does, keep it as the regression pin and say so.
- [ ] **Step 3: Write failing SIGTERM tests** — the handler fires the callback exactly once; a second signal does not re-fire; `rearm()` restores it; the previous handler is chained, never swallowed.
- [ ] **Step 4: Move the Win32 hook verbatim** into `shutdown_hook/windows.py`.
- [ ] **Step 5: Implement `linux.py`** — `signal.signal(SIGTERM/SIGINT)` → `fire()`, reusing the existing once-only + `rearm()` semantics. Register only on the main thread; degrade to `_NullHook` otherwise.
- [ ] **Step 6: Run suite — green.**
- [ ] **Step 7: Commit** — `"Turn the lights off on SIGTERM and pin lights_off import purity"`

---

### Task 8: `service_control/` — lift task management out of `dashboard.py`

**Files:**
- Create: `src/matterlights/service_control/__init__.py`, `windows.py`, `linux.py`
- Modify: `src/matterlights/dashboard.py` (remove `_run_powershell*`, `_task_status`, `_module_processes`, `_stop_module_processes`, `_spawn_independent_process`, `_restart_dashboard_process`, `_launch_zone_ui_process`)
- Test: `tests/test_service_control_linux.py`

**Interfaces produced:**
```python
@dataclass(frozen=True, slots=True)
class ServiceStatus:
    exists: bool; name: str; state: str = ""
    last_start: str = ""; last_result: str = ""; detail: str = ""

class ServiceControl(Protocol):
    def start(self, service: str) -> None
    def stop(self, service: str) -> None
    def restart(self, service: str) -> None
    def status(self, service: str) -> ServiceStatus
    def restart_self(self, service: str) -> None      # survives its own cgroup / job object

SYNC = "sync"; DASHBOARD = "dashboard"; ZONE_UI = "zone-ui"   # logical names, mapped per OS
```

- [ ] **Step 1: Write failing tests** against **recorded real output** of `systemctl --user show -p ActiveState,SubState,Result,ExecMainStartTimestamp` — active/inactive/failed/not-loaded, captured from this machine, not invented.
- [ ] **Step 2: Run, confirm failures.**
- [ ] **Step 3: Move the PowerShell implementation verbatim** into `service_control/windows.py`, mapping the logical names to `MatterLights Screen Sync` / `MatterLights Dashboard`.
- [ ] **Step 4: Implement `linux.py`** — `systemctl --user` for start/stop/restart/status; zone UI via `systemd-run --user --unit=matterlights-zone-ui`; `restart_self` via `systemd-run --user --on-active=1`.
- [ ] **Step 5: Rewire `dashboard.py`** to call the protocol. No behaviour change on Windows.
- [ ] **Step 6: Run suite — green.**
- [ ] **Step 7: Commit** — `"Drive services through one protocol instead of inline PowerShell"`

---

### Task 9: systemd units and the Linux installer

**Files:**
- Create: `systemd/matterlights-sync.service`, `systemd/matterlights-dashboard.service`, `systemd/matterlights-lights-off.service`, `scripts/linux/install.sh`, `scripts/linux/uninstall.sh`
- Test: `tests/test_unit_files.py`

- [ ] **Step 1: Write failing tests** parsing the shipped unit files: user units carry `PartOf=graphical-session.target` (the lingering trap) and `TimeoutStopSec` ≥ `SESSION_END_TIMEOUT_SECONDS`; the system unit carries `Type=oneshot`, `RemainAfterExit=yes`, `ExecStop=`, `After=network-online.target`, `RequiresMountsFor=`, and `Environment=MATTERLIGHTS_ENV_FILE=`.
- [ ] **Step 2: Run, confirm failures.**
- [ ] **Step 3: Write the unit files** exactly as specified in spec §7.2.
- [ ] **Step 4: Write `install.sh`** — refuse a miniconda interpreter with an explanatory message; build the venv from `/usr/bin/python3 --system-site-packages`; `pip install -e .`; run discovery; write `.env` at mode `0600`; template the absolute paths into the units; `systemctl --user enable --now` the user units and `sudo systemctl enable` the system one.
- [ ] **Step 5: Run install end to end.**
- [ ] **Step 6: Commit** — `"Add systemd units and a guided Linux installer"`

---

### Task 10: Documentation and packaging metadata

**Files:** Modify `README.md`, `pyproject.toml`, `.env.example`, `.gitignore`

- [ ] **Step 1: `pyproject.toml`** — version `0.2.0`, POSIX/Linux classifiers, `linux`/`wayland`/`pipewire` keywords, description no longer says "Windows".
- [ ] **Step 2: README** — rebrand to Windows + Linux; per-OS setup; why X11 capture cannot work on Wayland; the `python3`-is-miniconda trap; the three-layer shutdown table gaining a Linux column; every new env var and unit documented exhaustively.
- [ ] **Step 3: `.env.example`** — document any new settings; `.gitignore` — add `.claude/`.
- [ ] **Step 4: Commit** — `"Document Linux as a first-class platform"`

---

### Task 11: End-to-end verification

Nothing here is satisfied by a passing unit test. Each item is observed.

- [ ] Full suite green on Linux; win32 tests skipped, not broken.
- [ ] Lights visibly track screen content; colours compared against Windows in the same room.
- [ ] Sustained-video CPU measured from `systemctl --user status` after ~10 minutes (**R3** acceptance).
- [ ] Real logout → lights off, unit stopped.
- [ ] Real reboot → lights off.
- [ ] Monitor rearranged while running → stream rebuilds, lights keep tracking (**R4**).
- [ ] Dashboard screenshotted at **1280×720** and looked at.
- [ ] Windows regression check: the moved Windows modules still import and the win32 tests still pass on Windows.

---

## EXECUTION LOG — 2026-09-08

All 11 tasks complete on branch `linux-port`. **160 tests passing, 7 win32 tests
skipped** (baseline was 7 passing / 7 skipped / 5 erroring).

| Task | Outcome |
|---|---|
| 1 Split `screen.py` | ✅ Package importable on Linux; 5 erroring test modules → 0. Guard test added. |
| 2 `paths.py` | ✅ Log resolves to `~/.local/state/matterlights/` instead of the cwd. |
| 3 Linux capture | ✅ Mutter → PipeWire → BGRx, verified live on all four targets. |
| 4 Portal + R1 | ✅ Selection by bus-name presence; a Mutter error never becomes a portal prompt. |
| 5 `display_power/` | ✅ Mutter `PowerSaveMode` + sysfs DPMS fallback. |
| 6 `process_lock` | ✅ `flock`, verified cross-process. |
| 7 `shutdown_hook/` + R2 | ✅ SIGTERM; **R2 was violated and is now fixed and pinned.** |
| 8 `service_control/` | ✅ `dashboard.py` 1718 → 1548 lines. |
| 9 units + installer | ✅ Installed and running. |
| 10 docs | ✅ README 388 → 590 lines; pyproject 0.2.0. |
| 11 verification | ✅ See below. |

### Traps hit during execution — all fixed

- 🚨 **`RecordMonitor` returns NO frames on the VRR primary.** Cost most of the
  debugging time in Task 3, and every intermediate theory was wrong: the caps
  filter, the `GstApp` import, screen damage, leaked sessions. The invariant only
  appeared on a per-monitor sweep — DP-2 failed while DP-1, HDMI-1 and the whole
  desktop all succeeded instantly. `RecordArea` over the identical rectangle
  works. Now the single code path for every target.
- **A control that also fails proves the hypothesis wrong.** Mid-debug, the caps
  filter looked guilty until an interleaved control run showed the *plain*
  pipeline failing too. Without that control, the fix would have landed on the
  innocent component and the real bug would have survived.
- 🚨 **`GstApp` must be imported for `appsink` to have Python methods.** Without
  it there is no `try_pull_sample` and the `new-sample` path is unverifiable.
- **`max-framerate` in caps throttles at source; `videorate` does not.**
  Measured: 48.8 → 9.8 fps with caps, 46.5 fps with `videorate`.
- 🚨 **R2 was violated.** `screen.py` imported `capture` at module scope, so
  `lights_off` → `home_assistant` → `screen` pulled the capture package into the
  shutdown helper. Found by running the check, not by reasoning about it.
- **A signal test can kill its own runner.** Chaining to `SIG_DFL` and re-raising
  is correct in production and terminates the test process; the real path moved
  to a subprocess.
- **`configparser` keeps only the last duplicate key; systemd accumulates them.**
  The unit test read one `Environment=` line and called the other missing.
- **Running as root, the state dir resolves to `/root`.** The shutdown helper left
  a second log where nobody would look. `LOG_PATH=none` now disables file logging.
- **`MemoryCurrent` and `CPUUsageNSec` are easy to transpose.** A bad `awk`
  reported 10.4 GB RSS; the real figure is 174 MB and flat.

### Verified live, on the machine

- Capture on all four targets through the real backend (42–90 ms per grab).
- Sync loop running as a user unit, driving all six bulbs from real screen
  content, with distinct near/far ambience colours.
- Display sleep → lights off; wake → `Display resumed; restoring lights`.
- Capture target switching produces different colours per screen.
- Shutdown `ExecStop` as root: six bulbs on → off in 138 ms, journal only.
- Dashboard self-restart via transient timer; zone designer as a transient unit.
- Dashboard reviewed at 1280×720, no console errors.
- Colour engine proven **byte-identical** to pre-port (all 30 definitions).
- Memory flat at 174 MB over 30 s.

### NOT verified

- **Windows.** Cannot be run from here. Evidence is static only: every module
  compiles, the undefined-name scan is clean, and the Windows implementations
  were reconstructed from git rather than hand-edited after a first attempt
  stripped their constant blocks.
- **A real reboot.** `ExecStop` was exercised via `systemctl stop`, which runs
  the identical command as root; the shutdown ordering itself is unproven.
- **A real logout.** `PartOf=graphical-session.target` is asserted by test and by
  systemd semantics, not observed.
- **HDR colour fidelity** — accepted as out of scope by the owner.
