"""The lights hub: the mode and the screen as Home Assistant selects, and a status sensor.

Together with the two master switches (``ha_switches``) and manual control (``owner``), this lets one Home
Assistant dashboard hold every setting (the rest is ``ha_hub``).

* **Mode** (``HA_MODE_ENTITY``, an ``input_select`` with exactly the options in ``MODE_OPTIONS``): follow the screen,
  or play the colour / pattern saved in the control file. Mirrored both ways like a switch: the helper is shared by
  both halves of the dual boot, so on the first successful reading **Home Assistant wins**; after that whichever side
  moved since the agreed value is copied to the other.
* **Screen** (``HA_SCREEN_ENTITY``, an ``input_select`` whose options THIS machine writes): "Primary screen",
  "All screens" and one entry per monitor. Monitor numbers belong to one OS (Linux and Windows number the same
  screens differently), so here **the running machine wins** the first reading: it writes its own monitors as the
  options and its own choice (the per-OS control file) as the value. Set it only on the instance that captures the
  screen -- the TV's instance never gets it (``tv-hub``'s deploy copies an allow-list).
* **Status** (``HA_STATUS_ENTITY``, a state this process writes, like the heartbeat): what the loop is doing --
  following the screen, the saved colour or pattern, lamps off, display asleep, manual control, or which other
  instance has the lights. Posted when it changes and every ``STATUS_REFRESH_SECONDS`` (Home Assistant forgets such
  states when it restarts).

No reading is not a value: an unreachable Home Assistant, a missing entity or ``unknown``/``unavailable`` leaves both
sides alone until there is one. Runs on its own thread, never on the capture loop. The monitor list is handed in by
the capture loop (``offer_monitors``): Windows' capture library may not be called from another thread.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from matterlights.playback import (
    CUSTOM_PATTERN,
    CUSTOM_SOLID,
    MODE_AUTONOMOUS,
    MODE_CUSTOM,
    ControlState,
    effective_capture_target,
    load_control_state,
    update_control_selection,
)

LOGGER = logging.getLogger(__name__)

MODE_SCREEN = "Follow the screen"
MODE_SAVED_COLOUR = "Saved colour"
MODE_SAVED_PATTERN = "Saved pattern"
#: HA option -> (control-file mode, custom type or None)
MODE_OPTIONS: dict[str, tuple[str, str | None]] = {
    MODE_SCREEN: (MODE_AUTONOMOUS, None),
    MODE_SAVED_COLOUR: (MODE_CUSTOM, CUSTOM_SOLID),
    MODE_SAVED_PATTERN: (MODE_CUSTOM, CUSTOM_PATTERN),
}
SCREEN_PRIMARY = "Primary screen"
SCREEN_ALL = "All screens"
#: the only option on the portal path, where the user picked the screen in the screen-sharing dialog
SCREEN_DIALOG = "Picked in the screen-sharing dialog"
STATUS_REFRESH_SECONDS = 600.0


class SelectClient(Protocol):
    def get_state(self, entity_id: str) -> str | None: ...

    def select_option(self, entity_id: str, option: str) -> bool: ...

    def set_select_options(self, entity_id: str, options: list[str]) -> bool: ...

    def post_state(self, entity_id: str, state: str, attributes: dict) -> bool: ...


def mode_label(state: ControlState) -> str:
    if state.mode != MODE_CUSTOM:
        return MODE_SCREEN
    return MODE_SAVED_PATTERN if state.custom.type == CUSTOM_PATTERN else MODE_SAVED_COLOUR


def screen_choices(monitors: list[dict]) -> list[tuple[str, str]]:
    """(label, capture target) for each choice, in order. ``monitors`` as ``screen.list_monitors()`` returns them:
    index 0 is the whole desktop, 1.. the screens."""

    choices = [(SCREEN_PRIMARY, "primary"), (SCREEN_ALL, "all")]
    for m in monitors:
        index = int(m.get("index", 0))
        if index < 1:
            continue
        size = f"{m.get('width', '?')}×{m.get('height', '?')}"
        name = str(m.get("name") or "").strip()
        parts = [f"Screen {index}"] + ([name] if name and not name.startswith("\\\\") else []) + [size]
        if m.get("is_primary"):
            parts.append("primary")
        choices.append((" · ".join(parts), str(index)))
    return choices


class SelectMirror:
    """One synchronous pass per ``tick()``; a ``SwitchMirrorThread`` drives it (any object with ``tick()``)."""

    def __init__(
        self,
        control_state_file: Path,
        client: SelectClient,
        default_target: str,
        mode_entity: str = "",
        screen_entity: str = "",
        status_entity: str = "",
        logger: logging.Logger = LOGGER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = control_state_file
        self._client = client
        self._default_target = default_target
        self._mode_entity = mode_entity
        self._screen_entity = screen_entity
        self._status_entity = status_entity
        self._logger = logger
        self._clock = clock
        self._lock = threading.Lock()
        self._agreed_mode: str | None = None
        self._agreed_screen: str | None = None       # a capture target ("primary", "all", "2")
        self._monitors: list[dict] | None = None     # offered by the capture loop
        self._user_selected = False                  # the portal path: the user picked the screen, not a setting
        self._options_sent: list[str] | None = None
        self._status: tuple[str, dict] | None = None
        self._status_sent: tuple[str, dict] | None = None
        self._status_at = 0.0
        self._without_reading: set[str] = set()

    # -- fed from the capture loop -------------------------------------------------------------------------------
    def offer_monitors(self, monitors: list[dict], user_selected: bool = False) -> None:
        with self._lock:
            self._monitors = list(monitors)
            self._user_selected = user_selected

    def set_status(self, status: str, attributes: dict | None = None) -> None:
        with self._lock:
            self._status = (status, dict(attributes or {}))

    # -- the mirror's thread -------------------------------------------------------------------------------------
    def tick(self) -> None:
        if self._mode_entity:
            self._tick_mode()
        if self._screen_entity:
            self._tick_screen()
        if self._status_entity:
            self._tick_status()

    def _reading(self, entity_id: str) -> str | None:
        state = self._client.get_state(entity_id)
        if state is None:
            if entity_id not in self._without_reading:
                self._without_reading.add(entity_id)
                self._logger.warning("No reading from Home Assistant for %s; leaving it alone until there is one", entity_id)
        elif entity_id in self._without_reading:
            self._without_reading.discard(entity_id)
            self._logger.info("%s is readable again", entity_id)
        return state

    def _tick_mode(self) -> None:
        remote = self._reading(self._mode_entity)
        if remote is None:
            return
        local = mode_label(load_control_state(self._path))
        if remote not in MODE_OPTIONS:
            self._logger.warning("%s is %r, not one of %s; ignored", self._mode_entity, remote, list(MODE_OPTIONS))
            remote = None
        agreed = self._agreed_mode
        if agreed is None:                               # first reading: Home Assistant wins (the helper is shared)
            if remote is None:
                self._push_mode(local)
            elif remote != local:
                self._apply_mode(remote)
            else:
                self._agreed_mode = local
        elif local != agreed:                            # changed here (the control file): copy to HA
            if remote == local:
                self._agreed_mode = local
            else:
                self._push_mode(local)
        elif remote is not None and remote != agreed:    # changed in HA: copy here
            self._apply_mode(remote)

    def _apply_mode(self, label: str) -> None:
        mode, custom_type = MODE_OPTIONS[label]
        try:
            if custom_type is None:
                update_control_selection(self._path, mode=mode)
            else:
                update_control_selection(self._path, mode=mode, custom_type=custom_type)
        except ValueError as exc:
            # e.g. "Saved pattern" with no pattern saved: refuse, and show Home Assistant the mode that really runs
            local = mode_label(load_control_state(self._path))
            self._logger.warning("Cannot switch to %r (%s); keeping %r", label, exc, local)
            self._push_mode(local)
            return
        self._agreed_mode = label
        self._logger.info("Mode set to %r from Home Assistant", label)

    def _push_mode(self, label: str) -> None:
        if self._client.select_option(self._mode_entity, label):
            self._agreed_mode = label
            self._logger.info("Mode %r mirrored to %s", label, self._mode_entity)

    def _tick_screen(self) -> None:
        with self._lock:
            monitors = self._monitors
            user_selected = self._user_selected
        if monitors is None:
            return                                       # nothing offered yet: do not guess this machine's screens
        if user_selected:                                # no setting can choose: say so rather than offer a dead control
            if self._options_sent != [SCREEN_DIALOG] and self._client.set_select_options(self._screen_entity, [SCREEN_DIALOG]):
                self._options_sent = [SCREEN_DIALOG]
                self._agreed_screen = None
            return
        choices = screen_choices(monitors)
        labels = [label for label, _ in choices]
        by_label = dict(choices)
        by_target = {target: label for label, target in choices}
        if labels != self._options_sent:
            if not self._client.set_select_options(self._screen_entity, labels):
                return
            self._options_sent = labels
            self._agreed_screen = None                   # the options changed: the value is re-pushed below
        local_target = effective_capture_target(load_control_state(self._path), self._default_target)
        local_label = by_target.get(local_target)
        remote = self._reading(self._screen_entity)
        if self._agreed_screen is None:                  # first reading: this machine wins (screens are per OS)
            if local_label is not None and remote != local_label:
                if not self._client.select_option(self._screen_entity, local_label):
                    return
            self._agreed_screen = local_target
            return
        if local_target != self._agreed_screen:          # changed here
            if local_label is not None and remote != local_label:
                if not self._client.select_option(self._screen_entity, local_label):
                    return
            self._agreed_screen = local_target
        elif remote is not None and remote in by_label and by_label[remote] != self._agreed_screen:
            target = by_label[remote]                    # changed in HA
            update_control_selection(self._path, capture_target=target)
            self._agreed_screen = target
            self._logger.info("Screen set to %r from Home Assistant", remote)

    def _tick_status(self) -> None:
        with self._lock:
            status = self._status
        if status is None:
            return
        now = self._clock()
        if status == self._status_sent and now - self._status_at < STATUS_REFRESH_SECONDS:
            return
        text, attributes = status
        attributes = {"friendly_name": "MatterLights", "icon": "mdi:television-ambient-light", **attributes}
        if self._client.post_state(self._status_entity, text, attributes):
            self._status_sent = status
            self._status_at = now


def start_select_mirror(settings, client: SelectClient, logger: logging.Logger = LOGGER):
    """Start the mirror when any of HA_MODE_ENTITY / HA_SCREEN_ENTITY / HA_STATUS_ENTITY is set (and the control file
    is). Returns (mirror, thread) or None."""

    from matterlights.ha_switches import SwitchMirrorThread

    mode_entity = getattr(settings, "ha_mode_entity", "")
    screen_entity = getattr(settings, "ha_screen_entity", "")
    status_entity = getattr(settings, "ha_status_entity", "")
    if settings.control_state_file is None or not (mode_entity or screen_entity or status_entity):
        return None
    mirror = SelectMirror(
        settings.control_state_file,
        client,
        settings.screen_capture_target,
        mode_entity=mode_entity,
        screen_entity=screen_entity,
        status_entity=status_entity,
        logger=logger,
    )
    thread = SwitchMirrorThread(mirror, settings.ha_switch_poll_seconds, logger)
    thread.start()
    logger.info(
        "Lights hub: mode=%s screen=%s status=%s",
        mode_entity or "-", screen_entity or "-", status_entity or "-",
    )
    return mirror, thread
