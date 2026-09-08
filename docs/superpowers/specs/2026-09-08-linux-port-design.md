# MatterLights on Linux — design

**Date:** 2026-09-08
**Status:** approved design, pre-implementation
**Goal:** MatterLights runs on Linux with the same behaviour it has on Windows, and Windows
keeps working unchanged. The owner uses both halves of one dual-boot rig roughly equally;
a screen-sync agent that only exists on one of them is half a product.

---

## 1. Target environment

The reference Linux target is the Garuda half of `jxn-garuda`: **GNOME 50 on Wayland**, NVIDIA
610.57, three active displays (one 4K landscape primary, two 4K portrait), Home Assistant at
`192.168.10.2:8123`, six Ledvance Matter PAR16 RGBW bulbs — the same bulbs the Windows half
drives, in the same room.

Portability beyond that is deliberate but secondary: the capture layer has a portal-based
fallback so a non-GNOME desktop works, and nothing in the design assumes GNOME outside the
capture and display-power modules.

## 2. Measured facts this design rests on

Every one of these was verified on the target machine on 2026-09-08, not assumed.

| Fact | Evidence |
|---|---|
| `screen.py` does not import on Linux | `from ctypes import windll` at line 3 → `ImportError`. Cascades through `home_assistant` and `playback`, breaking 5 of 8 test modules. |
| Test baseline on Linux | 19 tests: **7 pass, 7 skip (win32-guarded), 5 error** — all 5 from the import above. |
| X11/mss capture is unusable | `mss` enumerates all three monitors correctly but every grabbed pixel is 0. XWayland knows the layout; it never receives the content. |
| `mss` geometry is wrong on Linux | Reports DP-2 as 7680×4320 against its real 3840×2160. Monitor enumeration must come from Mutter. |
| Mutter ScreenCast works end to end | `org.gnome.Mutter.ScreenCast` v4 → PipeWire node → GStreamer `appsink`: **3840×2160 BGRx, 14 ms to first frame, ~44 fps, no consent dialog, clean stop.** |
| It works from a systemd unit | Same result under `systemd-run --user`; the session env (`WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`) is imported into the user manager. |
| `BGRx` matches the existing sampler | The negotiated format is the exact byte order `_sample_zone` already indexes (`blue=+0, green=+1, red=+2`). No conversion. |
| `RecordArea(x,y,w,h)` exists | The "all screens combined" target is native; no per-monitor compositing needed. |
| Rotated outputs deliver display orientation | `RecordMonitor('DP-1')` on a `transform=3` output returns **2160×3840**, tall as displayed. Zone names stay physically correct. |
| 🚨 `RecordMonitor` yields NO frames on the VRR output | On DP-2 (Dell 32", 3840x2160@240, `refresh-rate-mode: variable`) `RecordMonitor` delivered nothing across repeated attempts, while `RecordArea` over the identical rectangle delivered a frame in <0.1 s. The two 60 Hz outputs worked either way. VRR is the only property that differs; **causation unconfirmed**. It is intermittent — the same call succeeded earlier the same day. |
| `RecordArea` works for every target | HDMI-1, DP-2, DP-1 and the whole desktop all delivered a frame in ~0.0 s at native resolution. |
| `max-framerate` throttles at source | Measured over 6 s on a live desktop: uncapped 293 frames (48.8 fps), `max-framerate=10/1` 59 frames (**9.8 fps**), `videorate max-rate=10` 279 frames (46.5 fps — drops downstream, does *not* throttle the source). |
| `GstApp` must be imported | Without `gi.require_version("GstApp", "1.0")` the appsink has no `try_pull_sample` and the `new-sample` path cannot be verified. |
| Portal supports restore tokens | `org.freedesktop.portal.ScreenCast` version **5** (≥4 required for `persist_mode`). |
| Display sleep is observable two ways | `Mutter.DisplayConfig.PowerSaveMode` (`i 0`) and `/sys/class/drm/*/dpms`. |
| PyGObject/GStreamer are system packages | Present on `/usr/bin/python3` (3.14.7). `python3` on PATH is **miniconda 3.13.7**, which cannot see them. |

## 3. Non-goals

- No change to any colour decision. Ambience palettes, near/far grouping, dark detection,
  saturation boost, zone geometry and the Home Assistant dispatch logic are untouched.
- No new streaming transport. Home Assistant stays the control plane.
- No attempt to share one `.env` between the two OSes (see §7.3).
- No support for X11 Linux sessions as a first-class path. `mss` capture would work there, but
  it is untested on this rig and is not what the machine runs.

---

## 4. Architecture

```
                    ┌──────────────── platform-free (unchanged logic) ────────────────┐
  capture backend → │  screen sampling · ambience · playback · home_assistant client  │ → HA
                    └────────────────────────────────────────────────────────────────┘
        ▲
        ├── windows: dxcam DXGI → mss/GDI fallback                     (moved verbatim)
        └── linux:   PipeWire → GStreamer appsink → BGRx frames        (new)
                       ├── node from org.gnome.Mutter.ScreenCast       (GNOME, no prompt)
                       └── node from xdg-desktop-portal + restore_token (non-GNOME)
```

### 4.1 Module layout

Each platform-coupled subsystem becomes a subpackage: `__init__.py` holds the protocol and the
factory, `windows.py` and `linux.py` hold the implementations. **Every existing import path
survives** because `__init__` re-exports the current names, so `main.py`, `dashboard.py`,
`zone_ui.py` and the test suite keep working.

```
src/matterlights/
  screen.py                  colour + zone engine only — platform-free, public API unchanged
  capture/
    __init__.py              CaptureBackend protocol, backend selection, Monitor list
    windows.py               dxcam + mss  (lifted from screen.py, logic unchanged)
    linux.py                 PipeWire → GStreamer frame pump
    mutter.py                node acquisition via org.gnome.Mutter.ScreenCast
    portal.py                node acquisition via xdg-desktop-portal (+ restore token)
  display_power/{__init__,windows,linux}.py
  shutdown_hook/{__init__,windows,linux}.py
  service_control/{__init__,windows,linux}.py    lifted out of dashboard.py
  process_lock.py            stays flat — small; gains an flock path
  paths.py                   NEW — per-OS state/log directories
```

Two files improve as a side effect, both of which serve this work rather than being unrelated
refactoring: `screen.py` (701 lines doing capture *and* sampling) splits along the seam the port
needs anyway, and `dashboard.py` (1717 lines) loses its embedded PowerShell process management to
`service_control/`.

---

## 5. Requirements

These are contract, not preference. Each is written because getting it wrong reproduces a
failure this project has already paid for once.

### R1 — Portal fallback is chosen by desktop identity, never by error

The backend selects the portal **only when `org.gnome.Mutter.ScreenCast` is not an activatable
name on the session bus.** On GNOME, a Mutter failure — a race with `gnome-shell` at login, a
dropped D-Bus call — is retried on `error_retry_seconds` and is **never** converted into a portal
request.

*Why:* a portal request raises a consent dialog. An autostarted unit that hits one at login hangs
on a dialog nobody is looking at, which is precisely the failure the Mutter path exists to avoid.
This is the same distinction the KB records from the Phase 6 pre-flight: *"a gate that turns empty
output into FAIL will confidently accuse the hardware when the real fault is permissions."*
Distinguish "this is not GNOME" from "GNOME answered badly".

### R2 — `lights_off` must not import the capture stack

`matterlights.lights_off` runs from a **system** unit, as root, during shutdown, with no session
bus. Its import chain must never reach `gi`, `Gst`, `mss` or `matterlights.capture`.

The current chain already violates this in spirit: `home_assistant.py:13` does
`from matterlights.screen import RgbColor`, and `screen.py` today imports the capture stack. The
split in §4.1 fixes it structurally — `screen.py` becomes platform-free — and a guard test pins it.

*Why:* the Windows docstring already fought this exact cost ("no screen capture, no mss/DXGI
import — every one of those costs import time the shutdown window does not have"). On Linux the
consequence is worse than slow: importing `gi` with no session bus can fail outright.

### R3 — The negotiated frame rate is capped

The caps filter carries `max-framerate`, computed as `ceil(2 / SYNC_INTERVAL_SECONDS)` frames per
second — twice the sampling rate, so a frame is always fresh without the stream free-running.
At the default `SYNC_INTERVAL_SECONDS=0.2` that is `10/1`. Verified to throttle Mutter at source:
110.7 fps → 4.7 fps. The value is derived at pipeline construction, never hard-coded.

*Why:* uncapped, `always-copy=true` on a 33 MB frame at 44 fps is ~1.4 GB/s of memcpy for frames
that are then sampled at `SAMPLE_STRIDE=121` and thrown away. Windows pays nothing comparable,
because dxcam is pulled on demand. Static screens are already free (the stream is damage-driven);
video and games are not.

**Acceptance is a measurement, not an assertion:** CPU time from `systemctl --user status` after
ten minutes of sustained video playback, compared against the same on Windows.

### R4 — Session, stream and monitor-layout changes are handled

The Linux backend subscribes to `Session.Closed`, `Stream.Closed` and
`DisplayConfig.MonitorsChanged`. On any of them it tears the session down and rebuilds it. If the
selected connector is gone, it logs a warning and falls back to `SCREEN_CAPTURE_TARGET`.

*Why:* this rig has five outputs at DRM level and three in the current layout; the owner
rearranges them. Without this, the first monitor toggle leaves the pipeline dead and the lights
frozen on the last frame forever — structurally the same latched-flag bug that
`enforce_lights_off` was written to fix, and the README already promises this exact recovery on
Windows.

---

## 6. Component specifications

### 6.1 `capture/`

**Protocol.** `CaptureBackend` exposes `grab(target) -> (raw_bgrx, width, height)`,
`list_monitors() -> list[Monitor]`, and `close()`. `screen.py`'s public functions keep their
signatures and delegate.

**Linux frame pump.** A GStreamer pipeline
`pipewiresrc path=N always-copy=true ! videoconvert ! video/x-raw,format=BGRx,max-framerate=10/1 !
appsink max-buffers=1 drop=true sync=false` runs on a dedicated thread with its own GLib main
loop. The `max-framerate` shown is the default-configuration value; it is derived per R3. An `appsink` callback stores the newest frame under a lock; `grab()` returns the latest, or
the previous frame when none has arrived.

That last behaviour is **identical to dxcam returning `None` on a static screen**, so the existing
"reuse the last frame rather than report black" logic — and the reason for it, keeping the lights
from blinking off on a still desktop — covers both platforms with one implementation.

**Monitor enumeration (Linux).** From `Mutter.DisplayConfig.GetCurrentState`. Logical monitors are
sorted by `(x, y)` and numbered `1..N`, so `SCREEN_CAPTURE_TARGET=2` and the dashboard picker are
stable across reboots. Index 0 is the virtual bounding box, matching the Windows convention.
**Linux and Windows monitor indices are unrelated**; each OS keeps its own control-state file.

**Capture targets — always `RecordArea`, never `RecordMonitor`.** `primary`, a numeric index and
`all` all resolve to a rectangle in stage coordinates, which is then recorded with `RecordArea`.

This is not the obvious design and it is not premature generality. `RecordMonitor` on this rig's
primary — the 240 Hz VRR gaming panel, which is precisely the screen this program exists to sync
to — delivered **no frames at all**, while `RecordArea` over that monitor's identical rectangle
delivered one immediately. It is intermittent, which is exactly why there is no fast path with a
fallback: a capture backend that works until the user starts a game is worse than useless, because
it fails in the only situation anyone cares about.

Because `RecordArea` takes coordinates rather than a connector, a stale rectangle would not error
after a monitor is unplugged — it would silently record whatever now occupies those coordinates.
So the target is resolved fresh on every rebuild, and R4's fallback triggers on the **connector**
disappearing, not on index resolution failing. Re-resolving an index cannot detect this: unplug the
middle screen of three and index 2 still resolves, just to a different monitor.

**Cursor.** `cursor-mode: 0` (hidden), matching dxcam.

**Portal path asymmetry.** With the portal, the *user* chooses the screen in the consent dialog,
so `SCREEN_CAPTURE_TARGET` cannot select it programmatically. On that path the dashboard's Capture
Screen panel reports that the source is portal-selected rather than silently showing a control
that does nothing. The `restore_token` is persisted through `paths.py` in the XDG state directory
— it is machine state, not configuration, and never goes in `.env`.

**`mss` remains a cross-platform dependency** solely for `mss.tools.to_png`, which is pure-Python
PNG encoding with no X11 dependency. Previews and the zone designer keep working unchanged.

### 6.2 `display_power/linux.py`

Subscribes to `PropertiesChanged` on `org.gnome.Mutter.DisplayConfig` and reads `PowerSaveMode`
(`0=on, 1=standby, 2=suspend, 3=off, -1=unknown`); anything other than `0` is "display off".
Falls back to polling `/sys/class/drm/*/dpms`, which is compositor-agnostic. Same `DisplayMonitor`
protocol and the same always-on degradation as Windows, so a failure never turns the lights off by
mistake.

### 6.3 `shutdown_hook/linux.py` and the lights-off unit

Three layers, mirroring the Windows story:

| Layer | Mechanism | Covers |
|---|---|---|
| In-process | `SIGTERM`/`SIGINT` handler → `turn_off_all_lights` | `systemctl --user stop`, logout (via `PartOf=`), Ctrl-C |
| System unit | `matterlights-lights-off.service`, `ExecStop` | shutdown, reboot |
| Home Assistant | heartbeat staleness automation (unchanged) | crash, hard reset, power cut |

**The Windows lesson transfers exactly.** On Windows it was proved that only a task running as
**SYSTEM in session 0** fires at shutdown, because the interactive session is already being
destroyed. The Linux mirror is precise: a **user** unit dies with the session, so the layer that
must survive shutdown is a **system** unit. Same conclusion, same reason — the thing being torn
down cannot be the thing that reports the teardown.

⚠️ **Lingering is enabled on this box** (for `claude-rc.service`). A user unit on `default.target`
would therefore survive logout: capture dead, lights stuck on. `PartOf=graphical-session.target`
is mandatory, not stylistic.

### 6.4 `service_control/`

Protocol: `start/stop/restart(service)` and `status(service) -> ServiceStatus`. Windows keeps the
existing Task Scheduler/PowerShell implementation, lifted out of `dashboard.py` unchanged. Linux
uses `systemctl --user`, with status parsed from
`systemctl --user show -p ActiveState,SubState,Result,ExecMainStartTimestamp`.

**Dashboard self-restart** uses `systemd-run --user --on-active=1 systemctl --user restart …`. The
transient unit lives outside the dashboard's own cgroup, so stopping the dashboard cannot kill the
helper that restarts it — exactly mirroring why the Windows version goes through WMI rather than
spawning a child.

**Zone designer** is launched as a transient unit (`systemd-run --user --unit=matterlights-zone-ui`)
rather than a detached child. This gives stop and status for free and removes process-matching
entirely on Linux. Where any process matching remains, it reads `/proc/*/cmdline` for an exact
`-m matterlights.zone_ui` token — **never `pgrep -f`**, which matches the searching shell's own
command line (a standing rule in this environment, and a bug that has cost real time here).

### 6.5 `paths.py`

| | Windows | Linux |
|---|---|---|
| Log | `%LOCALAPPDATA%\matterlights\matterlights.log` | `$XDG_STATE_HOME/matterlights/matterlights.log` (→ `~/.local/state/matterlights/`) |
| Portal restore token | n/a | `$XDG_STATE_HOME/matterlights/portal-restore-token` |

Today the non-Windows fallback is the current working directory, which litters the repo with logs.

---

## 7. Packaging, units and configuration

### 7.1 `pyproject.toml`

- `dxcam>=0.3.0` gains `; sys_platform == "win32"`. It installs on Linux (the wheel is pure Python)
  but cannot import there — it needs `comtypes`/COM — so it is dead weight in every Linux venv and
  an import-time trap for anything that reaches for it unguarded.
- Classifiers gain POSIX/Linux, keywords gain `linux`/`wayland`/`pipewire`, description stops
  saying "Windows".
- Version `0.1.0` → **`0.2.0`**. A cross-platform rebrand is a release.
- **PyGObject and GStreamer are deliberately not declared as pip dependencies.** They are system
  packages; declaring them would force a fragile source build. `capture/linux.py` raises a clear,
  actionable error naming the packages if `gi` is missing.

### 7.2 systemd units

`matterlights-sync.service` and `matterlights-dashboard.service` (user):

```ini
[Unit]
PartOf=graphical-session.target
After=graphical-session.target
[Service]
WorkingDirectory=%h/Projects/matterlights
TimeoutStopSec=20            # ≥ SESSION_END_TIMEOUT_SECONDS + margin
[Install]
WantedBy=graphical-session.target
```

`matterlights-lights-off.service` (system):

```ini
[Unit]
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/home/jxn/Projects/matterlights
[Service]
Type=oneshot
RemainAfterExit=yes
Environment=MATTERLIGHTS_ENV_FILE=/home/jxn/Projects/matterlights/.env
ExecStart=/bin/true
ExecStop=/home/jxn/Projects/matterlights/.venv/bin/python -m matterlights.lights_off
TimeoutStopSec=20
[Install]
WantedBy=multi-user.target
```

Both absolute paths are substituted by the installer from the actual checkout location.

systemd stops units in reverse dependency order, so `ExecStop` runs while the network is still up.
`RequiresMountsFor` ensures it stops before `@home` unmounts — the `.env` it needs lives there.

⚠️ **`MATTERLIGHTS_ENV_FILE` is required, not decorative.** This unit runs as root with a working
directory of `/`, so `.env` discovery by relative path finds nothing. It would currently limp along
on `config.py`'s `Path(__file__).parents[2]` fallback, which happens to land in the repo only
because the package is installed editable — a non-editable install would silently break the one
layer that guarantees the lights go off at shutdown. Be explicit instead of lucky.

### 7.3 Configuration

A **separate Linux `.env`**, seeded from the Windows one: same token, same six entities, same
ambience near-group. `LOG_PATH` left empty so it resolves per-OS; capture target defaults to the
primary. Mode `0600`.

Not shared via `/mnt/shared`: the Windows file carries `LOG_PATH=C:\…` and a Windows monitor index,
and neither is meaningful here.

### 7.4 Installer

`scripts/linux/install.sh`, the counterpart to `guided-setup.ps1`: create the venv **from
`/usr/bin/python3` with `--system-site-packages`**, refuse a miniconda interpreter with a message
explaining why (it cannot see system PyGObject), install the package, discover Home Assistant
entities, write `.env`, and offer to install and enable the units.

---

## 8. Testing

Baseline to beat: **7 pass / 7 skip / 5 error**. Target: all green with the win32 tests skipping
cleanly on Linux and the Linux tests skipping cleanly on Windows.

Two guard tests earn their place above ordinary coverage, because each pins a *class* of failure
rather than an instance:

1. **Import every module in the package on the current platform.** This is the class-level fix for
   `from ctypes import windll` at module scope — a platform-specific import that silently breaks
   the entire application on the other OS with no warning at build time. Modules named
   `*/windows.py` are expected to import only on win32 and `*/linux.py` only elsewhere; everything
   else must import everywhere.
2. **`lights_off` import purity.** Import `matterlights.lights_off` in a subprocess and assert
   `gi`, `Gst` and `mss` are absent from `sys.modules`. Pins R2.

Plus: Linux capture against a faked PipeWire/GStreamer layer; `flock` single-instance behaviour
(second acquirer returns `None`, kernel releases on death); SIGTERM → lights-off; `systemctl`
status parsing against recorded real output; monitor-index ordering; and R1's selection rule
(portal chosen only when the Mutter name is absent).

## 9. Documentation

Per the standing rule, exhaustive rather than illustrative. README rebranded to Windows + Linux
with per-OS setup, the Wayland capture story and why X11 capture cannot work, the three-layer
shutdown table gaining a Linux column, every new environment variable and every unit file
documented, and the `python3`-is-miniconda trap called out where someone will hit it.

## 10. Risks

| Risk | Mitigation |
|---|---|
| `org.gnome.Mutter.ScreenCast` is a private, unversioned API (v4) GNOME may change | The portal path is the hedge, and R1 keeps it from firing spuriously. A Mutter failure logs loudly rather than degrading in silence. |
| HDR primary: Mutter screencast is tone-mapped SDR | Compare a known scene against Windows by eye once. Parity means the same room looks the same, which is a judgement no test makes. |
| GStreamer/PipeWire adds a heavy dependency to the hot path | R3's cap, and a sustained-video CPU measurement as the acceptance gate. |
| Shutdown behaviour cannot be unit-tested honestly | Verified with a real logout and a real reboot. The Windows hook looked healthy and fired zero times across eight days; theory is not evidence here. |

## 11. Verification plan

Nothing is reported done on assertion alone.

1. Full test suite green on Linux; win32 tests skipped, not broken.
2. Lights visibly tracking screen content, checked against the Windows behaviour in the same room.
3. Sustained-video CPU measured from `systemctl --user status` and compared with Windows.
4. A real logout: lights go off, unit stops.
5. A real reboot: lights go off.
6. Monitor rearranged while running: stream rebuilds, lights keep tracking (R4).
7. Dashboard reviewed **by looking at a screenshot at 1280×720**, per the standing rule that UI is
   verified by looking and must fit 720p.
