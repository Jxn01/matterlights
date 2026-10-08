"""Mirror the dashboard's two master switches to Home Assistant, both ways.

The dashboard's **Lights** and **RGB** buttons write ``lights_on`` / ``rgb_on``
into the control-state file. With ``HA_LIGHTS_SWITCH_ENTITY`` and/or
``HA_RGB_SWITCH_ENTITY`` set, this mirror keeps each one equal to an on/off
entity in Home Assistant (an ``input_boolean`` helper), so the same switch can
be flipped from either place.

The rule, per switch, against the value both sides last agreed on:

* **First successful reading: Home Assistant wins.** The control file is per OS
  while the helper is shared, so whichever half of a dual-boot machine starts
  follows the switch as it was left -- from Home Assistant, from the other OS,
  or from a phone while the PC was off.
* After that, **whichever side moved since the agreed value is copied to the
  other.** There is no conflict case: a switch is a boolean, so if both sides
  moved off the agreed value they both landed on the same one.
* **No reading is not "off".** Home Assistant unreachable, the entity missing,
  ``unknown`` or ``unavailable``: the switch is left alone and retried.

It runs on its own thread, never on the capture loop -- a Home Assistant that
is slow to answer must not stall screen capture (see ``light_worker``).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from matterlights.playback import SWITCH_FIELDS, load_control_state, update_control_switches

LOGGER = logging.getLogger(__name__)


class SwitchClient(Protocol):
    def get_switch_state(self, entity_id: str) -> bool | None: ...

    def set_switch_state(self, entity_id: str, on: bool) -> bool: ...


@dataclass(frozen=True, slots=True)
class SwitchBinding:
    field: str
    """A ``ControlState`` attribute, one of ``SWITCH_FIELDS``."""
    entity_id: str


def switch_bindings(lights_entity: str, rgb_entity: str) -> list[SwitchBinding]:
    """The switches that are configured; an empty entity ID means "not mirrored"."""

    pairs = zip(SWITCH_FIELDS, (lights_entity, rgb_entity))
    return [SwitchBinding(name, entity_id) for name, entity_id in pairs if entity_id]


class SwitchMirror:
    """One synchronous pass per ``tick()``; ``SwitchMirrorThread`` drives it."""

    def __init__(
        self,
        bindings: list[SwitchBinding],
        control_state_file: Path,
        client: SwitchClient,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._bindings = list(bindings)
        self._path = control_state_file
        self._client = client
        self._logger = logger
        # The value both sides last agreed on. None until the first successful
        # Home Assistant reading, which is what makes that reading win.
        self._agreed: dict[str, bool | None] = {b.field: None for b in self._bindings}
        self._without_reading: set[str] = set()

    def agreed(self, field: str) -> bool | None:
        return self._agreed[field]

    def tick(self) -> None:
        local = load_control_state(self._path)
        from_home_assistant: dict[str, bool] = {}
        for binding in self._bindings:
            remote = self._client.get_switch_state(binding.entity_id)
            if not self._note_reading(binding, remote):
                continue
            current = getattr(local, binding.field)
            agreed = self._agreed[binding.field]
            if agreed is None:
                if remote != current:
                    from_home_assistant[binding.field] = remote
                else:
                    self._agreed[binding.field] = remote
            elif current != agreed:
                if remote == current:
                    self._agreed[binding.field] = current
                elif self._client.set_switch_state(binding.entity_id, current):
                    self._agreed[binding.field] = current
                    self._logger.info(
                        "%s switched %s from the dashboard; mirrored to %s",
                        _label(binding.field), _word(current), binding.entity_id,
                    )
                # A failed push leaves `agreed` alone, so the next tick retries.
            elif remote != agreed:
                from_home_assistant[binding.field] = remote

        if from_home_assistant:
            # Read-modify-write of the switches alone, at write time.
            update_control_switches(self._path, from_home_assistant)
            for name, value in from_home_assistant.items():
                self._agreed[name] = value
                self._logger.info("%s switched %s from Home Assistant", _label(name), _word(value))

    def _note_reading(self, binding: SwitchBinding, remote: bool | None) -> bool:
        """Log the moment a switch loses or regains its reading; True when readable."""

        if remote is None:
            if binding.field not in self._without_reading:
                self._without_reading.add(binding.field)
                self._logger.warning(
                    "No reading from Home Assistant for %s (unreachable, missing, unknown or "
                    "unavailable); leaving %s alone until there is one",
                    binding.entity_id, _label(binding.field),
                )
            return False
        if binding.field in self._without_reading:
            self._without_reading.discard(binding.field)
            self._logger.info("%s is readable again", binding.entity_id)
        return True


class SwitchMirrorThread:
    def __init__(self, mirror: SwitchMirror, interval_seconds: float, logger: logging.Logger = LOGGER) -> None:
        self._mirror = mirror
        self._interval = interval_seconds
        self._logger = logger
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="matterlights-ha-switches", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Never holds up shutdown: the session-end path has seconds to spare,
        # and a tick blocked on Home Assistant can simply be abandoned.
        self._stop.set()
        self._thread.join(timeout=0.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._mirror.tick()
            except Exception:  # noqa: BLE001 -- the mirror must outlive any one failure
                self._logger.exception("Home Assistant switch mirror failed; retrying")
            self._stop.wait(self._interval)


def start_switch_mirror(settings, client: SwitchClient, logger: logging.Logger = LOGGER) -> SwitchMirrorThread | None:
    """Start mirroring when at least one switch entity and the control file are set."""

    bindings = switch_bindings(settings.ha_lights_switch_entity, settings.ha_rgb_switch_entity)
    if not bindings or settings.control_state_file is None:
        return None
    mirror = SwitchMirrorThread(
        SwitchMirror(bindings, settings.control_state_file, client, logger),
        settings.ha_switch_poll_seconds,
        logger,
    )
    mirror.start()
    logger.info(
        "Mirroring switches to Home Assistant: %s",
        ", ".join(f"{_label(b.field)}={b.entity_id}" for b in bindings),
    )
    return mirror


def _label(field: str) -> str:
    return {"lights_on": "Lights", "rgb_on": "RGB"}.get(field, field)


def _word(on: bool) -> str:
    return "on" if on else "off"
