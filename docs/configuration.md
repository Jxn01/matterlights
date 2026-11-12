# Configuration

Settings come from four places, highest precedence first:

1. the process environment (a shell variable or a systemd unit's `Environment=`);
2. this OS's overlay, `.env.linux` or `.env.windows`, beside `.env`;
3. `.env`;
4. the built-in default below.

An empty value means "not set here" and falls through to the next source, so `LOG_PATH=` in an overlay restores the
per-OS default. `.env` is found at `MATTERLIGHTS_ENV_FILE` if set (the systemd units set it), else in the working
directory, else in the checkout. Relative paths resolve from the directory `.env` is in. Copy `.env.example` to start.

## One checkout, two operating systems

A checkout can serve both halves of a dual-boot machine (for example on a drive both mount). Everything that differs
per OS is kept apart by name:

| What | Shared | Per OS |
| --- | --- | --- |
| Settings | `.env` — the token, the bulbs, the zones | `.env.linux` / `.env.windows` — e.g. `RGB_PUBLISH_SOCKET`, `LOG_PATH`, `SAMPLE_STRIDE` |
| Zone layout, playback state | — | `.matterlights-zones.<os>.json`, `.matterlights-control.<os>.json` |
| Virtualenv | — | Windows: `.venv` in the checkout. Linux: `~/.local/share/venvs/matterlights` |
| Log, portal restore token | — | the OS's state directory (see `LOG_PATH`) |

Never set `LIGHT_ZONE_FILE` or `CONTROL_STATE_FILE` in a shared `.env`: an explicit value beats the per-OS default, so
both systems would read and overwrite the same file. A state file with the older name without `<os>` is still used
while the OS has no file of its own.

## Every setting

| Variable | Default | Purpose |
| --- | --- | --- |
| `HA_URL` | `http://192.168.10.2:8123` | Base URL of Home Assistant. |
| `HA_TOKEN` | *(required)* | Home Assistant long-lived access token. |
| `HA_LIGHT_ENTITIES` | *(required by the sync loop; `discover` runs without it)* | Comma-separated list of light entities to control. |
| `LIGHT_ZONE_LAYOUT` | empty | Ordered zone names matched one-to-one to `HA_LIGHT_ENTITIES`. |
| `LIGHT_ZONE_FILE` | `.matterlights-zones.<os>.json` beside `.env` | The saved zone layout; the zone selects in Home Assistant write it. Without it, `LIGHT_ZONE_LAYOUT` applies. |
| `CONTROL_STATE_FILE` | `.matterlights-control.<os>.json` beside `.env` | Where the playback mode (autonomous/custom) and pattern are stored. |
| `RESPECT_DISPLAY_SLEEP` | `true` | `true` to turn the lights off when the monitor sleeps. |
| `TURN_OFF_ON_SHUTDOWN` | `true` | `true` to turn the lights off at shutdown, restart or logoff. |
| `RGB_EXTENSION_ENABLED` | `false` | Publish frames to the local RGB extension (ambience-rgb). Off: nothing is published. |
| `RGB_PUBLISH_SOCKET` | empty | Where to publish: a unix datagram socket path (Linux), or `udp://127.0.0.1:PORT` (Windows, which has no unix datagram sockets). A per-OS value -- put it in the overlay. |
| `HEARTBEAT_ENTITY_ID` | `sensor.matterlights_heartbeat` | Entity the sync loop refreshes so Home Assistant can notice the PC is gone (crash, hard reset, power cut). Empty disables it. |
| `HEARTBEAT_INTERVAL_SECONDS` | `30.0` | How often the heartbeat is refreshed. |
| `HA_LIGHTS_SWITCH_ENTITY` | *(empty: not mirrored)* | On/off entity in Home Assistant kept equal to the control file's **Lights** switch, both ways. Must be an entity ID (`domain.name`). |
| `HA_RGB_SWITCH_ENTITY` | *(empty: not mirrored)* | Same, for the **RGB** switch. |
| `HA_SWITCH_POLL_SECONDS` | `1.0` | How often the switch mirror polls Home Assistant and the control file. Must be greater than 0. |
| `HA_OWNER_ENTITY` | *(empty: always the owner)* | Entity in Home Assistant naming which instance drives the bulbs. See the README → *Two machines, one set of bulbs*. |
| `HA_OWNER_VALUE` | *(empty)* | The value of `HA_OWNER_ENTITY` that means this instance. Required with it. |
| `HA_MANUAL_ENTITY` | *(empty: no manual control)* | An on/off helper (`input_boolean.lamps_manual`). While it is **on**, this instance leaves the bulbs entirely alone — no colours, no switching them off (master switch, display sleep, shutdown) — so a colour set in Home Assistant stays. Set it on **every** instance. |
| `HA_MODE_ENTITY` | *(empty: not mirrored)* | An `input_select` with exactly the options *Follow the screen*, *Saved colour*, *Saved pattern*, mirrored both ways with the control file's mode (Home Assistant wins the first reading). |
| `HA_SCREEN_ENTITY` | *(empty: not mirrored)* | An `input_select` whose options this machine writes — *Primary screen*, *All screens* and one entry per monitor — mirrored both ways with the control file's capture screen (this machine wins the first reading: monitor numbers are per OS). Only on the instance that captures the screen; set it in `.env.linux` / `.env.windows`. |
| `HA_STATUS_ENTITY` | *(empty: no status)* | An entity id this process writes (like the heartbeat): what the loop is doing — *Following the screen*, *Saved colour*, *Saved pattern*, *Lamps off*, *Display asleep*, *Manual control*, *<owner> has the lamps*, *Starting* — with the attributes `mode`, `screen`, `os`, `engine`, `lights_available`, `lights_unavailable`, `recent_problems`, `setting_problems`. Posted on change and every 10 minutes. Only on the screen instance (`.env.<os>`). |
| `HA_HUB_PREFIX` | *(empty: not mirrored)* | Prefix of the hub helpers (`matterlights` → `input_number.matterlights_brightness`, …): the saved colour, white, brightness and pattern, the colour engine, each light's zone and a restart button, mirrored both ways (Home Assistant wins the first reading). Letters, digits and `_` only. |
| `CAPTURE_SOURCE` | `screen` | `screen`, or `tv` for frames from an LG TV. |
| `TV_FRAME_BIND` | `0.0.0.0` | Address the TV source listens on. |
| `TV_FRAME_PORT` | `8792` | UDP port the TV source listens on. |
| `TV_FRAME_SENDER` | *(empty)* | The TV's address; packets from anyone else are ignored. Required with `CAPTURE_SOURCE=tv`. |
| `COLOR_SYNC_MODE` | `zoned` | `ambience` (recommended), `zoned`, or `shared-variant`. |
| `PRIMARY_LIGHT_ZONE_NAMES` | `top-center,bottom-left` | Primary bulbs used in shared-variant mode. |
| `AMBIENCE_NEAR_LIGHTS` | empty | Entity IDs of the bulbs beside the screen (ambience mode's near group). |
| `SCREEN_CAPTURE_TARGET` | `primary` | Default screen: `primary`, `all`, or a 1-based monitor index. Overridable live from Home Assistant's Screen select. **Indices are per-OS and unrelated**: Windows numbers them in `mss` enumeration order, Linux sorts Mutter's logical monitors by position. Ignored on the portal capture path, where the user picks the screen in the consent dialog. |
| `REQUEST_TIMEOUT_SECONDS` | `3.0` | Timeout of each Home Assistant request from the sync loop. |
| `SESSION_END_TIMEOUT_SECONDS` | `10.0` | How long the session-end turn-off waits for Home Assistant. Longer than `REQUEST_TIMEOUT_SECONDS` because it gets one attempt and no retry. |
| `AVAILABILITY_REFRESH_SECONDS` | `5.0` | How often the sync loop re-reads which bulbs Home Assistant reports available. |
| `SYNC_INTERVAL_SECONDS` | `0.2` | Capture cadence. Lower is faster and heavier. |
| `INTER_LIGHT_DELAY_SECONDS` | `0.4` | Pause between consecutive bulb updates within one batch. |
| `MAX_PARALLEL_LIGHT_UPDATES` | `1` | Upper bound for concurrent Home Assistant light updates. |
| `COLOR_CHANGE_THRESHOLD` | `12` | How far a colour must move before a bulb is sent it again. |
| `LIGHT_UPDATE_INTERVAL_SECONDS` | `0.0` | Minimum gap between Home Assistant writes, independent of the sync rate -- lets the RGB extension run fast without asking Matter bulbs for more than they can take. `0.0` writes whenever the colour moves enough. |
| `BRIGHTNESS_FLOOR` | `0` | Minimum brightness on active updates. |
| `TRANSITION_SECONDS` | `0.0` | Transition sent with sync-loop updates and turn-offs. |
| `MAX_PATTERN_TRANSITION_SECONDS` | `0.0` | Caps custom pattern fades; `0` snaps (safe for Matter bulbs that freeze on transitions). |
| `DARK_THRESHOLD` | `12` | Average brightness at or below which the screen counts as dark. |
| `DARK_ACTIVE_RATIO_THRESHOLD` | `0.05` | Share of non-dark pixels at or below which a dark screen turns the lights off. |
| `COLOR_BOOST` | `1.15` | Saturation multiplier applied after sampling. |
| `SAMPLE_STRIDE` | `24` | Pixel stride when sampling a frame: higher is cheaper and coarser. |
| `ERROR_RETRY_SECONDS` | `5.0` | Back-off after a batch of bulb updates fails. |
| `LOG_PATH` | per-OS state dir | Log file. Defaults per-OS: `%LOCALAPPDATA%\matterlights\matterlights.log` on Windows, `$XDG_STATE_HOME/matterlights/matterlights.log` (usually `~/.local/state/matterlights/`) on Linux. `none`, `off` or `-` log to stdout only. Keep an explicit path in the OS's overlay, not in a shared `.env`. |
| `MATTERLIGHTS_ENV_FILE` | *(environment only)* | Path of `.env`; its overlay is read from beside it. Set by the systemd units. |

`tests/test_readme_settings.py` fails the build when `config.py` reads a setting this table does not list.

## Zone names

`full` · `top-left`, `top-center`, `top-right` · `right-top`, `right-center`, `right-bottom` · `bottom-right`,
`bottom-center`, `bottom-left` · `left-bottom`, `left-center`, `left-top` · `center`

```dotenv
LIGHT_ZONE_LAYOUT=top-left,top-center,top-right,bottom-right,bottom-center,bottom-left
```
