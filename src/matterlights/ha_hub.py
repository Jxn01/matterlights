"""The lights hub, part two (2026-11-12): every setting the localhost pages held, as Home Assistant helpers.

The owner: *"I want no configurable anything remaining in localhost, everything should move into HA."* With
``HA_HUB_PREFIX`` set (e.g. ``matterlights``) this mirrors, both ways, these helpers (``<p>`` = the prefix):

=====================================  ======================================================================
``input_number.<p>_brightness``        the saved colour / pattern brightness, 1-100 % (control file 1-255)
``input_select.<p>_colour_type``       ``Colour`` / ``White`` -- what the saved colour is
``input_text.<p>_colour``              the saved colour, ``#rrggbb``
``input_number.<p>_kelvin``            the saved white, 1500-8000 K
``input_text.<p>_pattern``             the saved pattern as one line: ``#ff3300 2 1, 2700K 3 0.5`` (colour or
                                       white, hold seconds, optional transition seconds; ``,`` between steps)
``input_select.<p>_engine``            the colour engine: ``Ambience`` / ``Zoned`` / ``Shared variant`` (live)
``input_select.<p>_zone_<n>``          the screen zone of light *n* (``HA_LIGHT_ENTITIES`` order, from 1): a
                                       preset name, or ``custom`` for a zone drawn by hand in the zones file
``input_button.<p>_restart``           restart the sync loop (the process replaces itself)
=====================================  ======================================================================

Same rule as the other selects (``ha_selects``): the helpers are shared by both halves of a dual boot, so on the first
successful reading **Home Assistant wins**; after that whichever side moved since the agreed value is copied to the
other. A value Home Assistant holds that cannot be applied (a mistyped pattern) is kept there untouched, reported in
``problems()`` (the status sensor's ``setting_problems`` attribute), and applied as soon as it is corrected. No reading is not a
value. The restart button fires on a press made after this process started: an input_button's state is the time of
its last press (``unknown`` until the first one), so an old press never restarts it, the very first press does, and
the restarted copy cannot loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from matterlights.playback import (
    COLOR_RGB,
    COLOR_WHITE,
    COLOR_SYNC_MODES,
    format_pattern,
    load_control_state,
    parse_pattern,
    update_control_selection,
)
from matterlights.screen import _ZONE_PRESETS, RgbColor, ScreenZone, load_configured_light_zones, save_light_zones

LOGGER = logging.getLogger(__name__)

ENGINE_LABELS = {"ambience": "Ambience", "zoned": "Zoned", "shared-variant": "Shared variant"}
COLOUR_TYPES = {COLOR_RGB: "Colour", COLOR_WHITE: "White"}
ZONE_CUSTOM = "custom"
ZONE_OPTIONS = sorted(_ZONE_PRESETS) + [ZONE_CUSTOM]
#: Home Assistant's limit for an input_text value
TEXT_MAX = 255


class HubClient(Protocol):
    def get_state(self, entity_id: str) -> str | None: ...

    def call_service(self, domain: str, service: str, data: dict) -> bool: ...


@dataclass
class Binding:
    entity_id: str
    local: Callable[[], str]
    """The local value, as Home Assistant would show it."""
    apply: Callable[[str], None]
    """Write a Home Assistant value here; raises ValueError when it cannot be applied."""
    push: Callable[[str], bool]
    """Write a local value to Home Assistant."""
    same: Callable[[str, str], bool] = field(default=lambda a, b: a == b)


def hub_entities(prefix: str, light_count: int) -> dict[str, str]:
    """Every helper entity id the hub uses, by role."""

    entities = {
        "brightness": f"input_number.{prefix}_brightness",
        "colour_type": f"input_select.{prefix}_colour_type",
        "colour": f"input_text.{prefix}_colour",
        "kelvin": f"input_number.{prefix}_kelvin",
        "pattern": f"input_text.{prefix}_pattern",
        "engine": f"input_select.{prefix}_engine",
        "restart": f"input_button.{prefix}_restart",
    }
    for n in range(1, light_count + 1):
        entities[f"zone_{n}"] = f"input_select.{prefix}_zone_{n}"
    return entities


def _hex(colour: RgbColor) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour.as_list())


def _parse_hex(text: str) -> RgbColor:
    hexa = text.strip().lower().lstrip("#")
    if len(hexa) != 6 or any(c not in "0123456789abcdef" for c in hexa):
        raise ValueError(f"{text!r} is not a #rrggbb colour")
    return RgbColor(int(hexa[0:2], 16), int(hexa[2:4], 16), int(hexa[4:6], 16))


def _same_number(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(b)) < 0.5
    except ValueError:
        return False


def _same_pattern(a: str, b: str) -> bool:
    if a.strip() == b.strip():
        return True
    try:
        return parse_pattern(a) == parse_pattern(b)
    except ValueError:
        return False


class HubMirror:
    def __init__(
        self,
        settings,
        client: HubClient,
        on_restart: Callable[[], None],
        logger: logging.Logger = LOGGER,
        started: datetime | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._on_restart = on_restart
        self._logger = logger
        self._path: Path = settings.control_state_file
        self._entities = hub_entities(settings.ha_hub_prefix, len(settings.light_entities))
        self._agreed: dict[str, str] = {}
        self._failed: dict[str, str] = {}           # entity -> the HA value that could not be applied
        self._problems: dict[str, str] = {}         # entity -> message
        self._local_failed: set[str] = set()        # entities whose local value could not be read
        self._without_reading: set[str] = set()
        self._restart_seen: str | None = None
        self._started = started or datetime.now(timezone.utc)
        self._state_cache = None                    # the control file, parsed once per tick (cleared after a write)
        self._zones_cache: list[ScreenZone] | None = None
        self._bindings = self._build_bindings()

    # -- what the status sensor shows -------------------------------------------------------------------------------
    def problems(self) -> dict[str, str]:
        return dict(self._problems)

    # -- bindings -----------------------------------------------------------------------------------------------------
    def _set(self, domain: str, service: str, entity_id: str, **data) -> bool:
        return self._client.call_service(domain, service, {"entity_id": entity_id, **data})

    def _zones(self) -> list[ScreenZone]:
        if self._zones_cache is None:
            s = self._settings
            self._zones_cache = load_configured_light_zones(s.light_zone_layout, list(s.light_entities), s.light_zone_file)
        return list(self._zones_cache)              # a copy: a zone apply edits the list it gets

    def _state(self):
        if self._state_cache is None:
            self._state_cache = load_control_state(self._path)
        return self._state_cache

    def _forget_local(self) -> None:
        self._state_cache = None
        self._zones_cache = None

    def _zone_binding(self, index: int) -> Binding:
        entity_id = self._entities[f"zone_{index + 1}"]

        def local() -> str:
            zone = self._zones()[index]
            preset = _ZONE_PRESETS.get(zone.name)
            if preset is not None and all(abs(a - b) < 1e-6 for a, b in zip(preset, (zone.left, zone.top, zone.right, zone.bottom))):
                return zone.name
            return ZONE_CUSTOM

        def apply(value: str) -> None:
            if value == ZONE_CUSTOM:
                raise ValueError("a custom zone is drawn by hand in the zones file; pick a preset to replace it")
            if value not in _ZONE_PRESETS:
                raise ValueError(f"{value!r} is not a zone preset")
            if self._settings.light_zone_file is None:
                raise ValueError("LIGHT_ZONE_FILE is not set")
            zones = self._zones()
            zones[index] = ScreenZone(value, *_ZONE_PRESETS[value])
            save_light_zones(self._settings.light_zone_file, list(self._settings.light_entities), zones)

        return Binding(entity_id, local, apply, lambda v: self._set("input_select", "select_option", entity_id, option=v))

    def _build_bindings(self) -> list[Binding]:
        e = self._entities
        state = self._state
        default_engine = self._settings.color_sync_mode

        def engine_local() -> str:
            return ENGINE_LABELS[state().color_sync_mode or default_engine]

        def engine_apply(label: str) -> None:
            mode = {v: k for k, v in ENGINE_LABELS.items()}.get(label)
            if mode not in COLOR_SYNC_MODES:
                raise ValueError(f"{label!r} is not a colour engine")
            # the environment's engine is stored as "inherit", so a later COLOR_SYNC_MODE change still applies
            update_control_selection(self._path, color_sync_mode=None if mode == default_engine else mode)

        def brightness_apply(value: str) -> None:
            pct = float(value)
            update_control_selection(self._path, brightness=max(1, min(255, round(pct * 255 / 100))))

        def type_apply(label: str) -> None:
            mode = {v: k for k, v in COLOUR_TYPES.items()}.get(label)
            if mode is None:
                raise ValueError(f"{label!r} is neither Colour nor White")
            update_control_selection(self._path, solid_mode=mode)

        def pattern_apply(text: str) -> None:
            update_control_selection(self._path, pattern_steps=parse_pattern(text))

        def pattern_push(text: str) -> bool:
            if len(text) > TEXT_MAX:                    # an old pattern saved by the retired editor, 24 steps long
                self._problems[e["pattern"]] = (
                    f"the saved pattern is {len(text)} characters, more than Home Assistant's {TEXT_MAX}; "
                    "type a shorter one in Home Assistant to replace it")
                return False
            self._problems.pop(e["pattern"], None)
            return self._set("input_text", "set_value", e["pattern"], value=text)

        bindings = [
            Binding(e["brightness"], lambda: str(round(state().custom.brightness * 100 / 255)), brightness_apply,
                    lambda v: self._set("input_number", "set_value", e["brightness"], value=float(v)), _same_number),
            Binding(e["colour_type"], lambda: COLOUR_TYPES[state().custom.solid_mode], type_apply,
                    lambda v: self._set("input_select", "select_option", e["colour_type"], option=v)),
            Binding(e["colour"], lambda: _hex(state().custom.solid_color),
                    lambda v: update_control_selection(self._path, solid_color=_parse_hex(v)),
                    lambda v: self._set("input_text", "set_value", e["colour"], value=v),
                    lambda a, b: a.strip().lower().lstrip("#") == b.strip().lower().lstrip("#")),
            Binding(e["kelvin"], lambda: str(state().custom.solid_kelvin),
                    lambda v: update_control_selection(self._path, solid_kelvin=int(round(float(v)))),
                    lambda v: self._set("input_number", "set_value", e["kelvin"], value=float(v)), _same_number),
            Binding(e["pattern"], lambda: format_pattern(state().custom.pattern_steps), pattern_apply,
                    pattern_push, _same_pattern),
            Binding(e["engine"], engine_local, engine_apply,
                    lambda v: self._set("input_select", "select_option", e["engine"], option=v)),
        ]
        bindings += [self._zone_binding(i) for i in range(len(self._settings.light_entities))]
        return bindings

    # -- the thread -----------------------------------------------------------------------------------------------------
    def _read_all(self) -> dict[str, str | None]:
        ids = [b.entity_id for b in self._bindings] + [self._entities["restart"]]
        batch = getattr(self._client, "get_states", None)
        if batch is not None:
            return batch(ids)                       # one request for all of them
        return {entity_id: self._client.get_state(entity_id) for entity_id in ids}

    def _reading(self, entity_id: str, value: str | None) -> str | None:
        if value is None:
            if entity_id not in self._without_reading:
                self._without_reading.add(entity_id)
                self._logger.warning("No reading from Home Assistant for %s; leaving it alone until there is one", entity_id)
        elif entity_id in self._without_reading:
            self._without_reading.discard(entity_id)
            self._logger.info("%s is readable again", entity_id)
        return value

    def tick(self) -> None:
        self._forget_local()
        remote = self._read_all()
        for binding in self._bindings:
            self._tick_binding(binding, remote.get(binding.entity_id))
        self._tick_restart(remote.get(self._entities["restart"]))

    def _tick_binding(self, b: Binding, value: str | None) -> None:
        remote = self._reading(b.entity_id, value)
        if remote is None:
            return
        try:
            local = b.local()
        except (ValueError, KeyError) as exc:          # e.g. a zones file that does not load
            self._problems[b.entity_id] = str(exc)
            self._local_failed.add(b.entity_id)
            return
        if b.entity_id in self._local_failed:           # readable again: that problem is over
            self._local_failed.discard(b.entity_id)
            self._problems.pop(b.entity_id, None)
        agreed = self._agreed.get(b.entity_id)
        if agreed is None:                              # first reading: Home Assistant wins
            if b.same(remote, local):
                self._agree(b, local)
            else:
                self._apply(b, remote, local)
        elif not b.same(local, agreed):                 # changed here: copy to Home Assistant
            if b.same(remote, local) or b.push(local):
                self._agree(b, local)
        elif not b.same(remote, agreed):                # changed in Home Assistant: copy here
            self._apply(b, remote, local)

    def _agree(self, b: Binding, value: str) -> None:
        """Both sides hold ``value``: whatever could not be applied before no longer matters."""

        self._agreed[b.entity_id] = value
        self._failed.pop(b.entity_id, None)
        self._problems.pop(b.entity_id, None)

    def _apply(self, b: Binding, remote: str, local: str) -> None:
        try:
            b.apply(remote)
        except ValueError as exc:
            self._forget_local()
            if self._failed.get(b.entity_id) != remote:
                self._failed[b.entity_id] = remote
                self._logger.warning("Cannot apply %s = %r: %s", b.entity_id, remote, exc)
            self._problems[b.entity_id] = str(exc)
            if b.entity_id not in self._agreed:
                self._agreed[b.entity_id] = local       # first reading: keep both sides until HA is corrected
            return
        self._forget_local()                        # the file just changed: read it again
        self._agree(b, b.local())
        self._logger.info("%s set to %r from Home Assistant", b.entity_id, remote)

    def _tick_restart(self, pressed: str | None) -> None:
        # Not self._reading(): a button never pressed is "unknown", which is its normal state, not a missing reading.
        if pressed is None or pressed == self._restart_seen:
            return
        self._restart_seen = pressed
        try:
            at = datetime.fromisoformat(pressed)
        except ValueError:
            self._logger.warning("%s is %r, not a press time; ignored", self._entities["restart"], pressed)
            return
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at > self._started:
            self._logger.info("Restart requested from Home Assistant (pressed %s)", pressed)
            self._on_restart()


def start_hub_mirror(settings, client: HubClient, on_restart: Callable[[], None], logger: logging.Logger = LOGGER):
    """Start the hub mirror when HA_HUB_PREFIX and the control file are set; returns (mirror, thread) or None."""

    from matterlights.ha_switches import SwitchMirrorThread

    if not getattr(settings, "ha_hub_prefix", "") or settings.control_state_file is None:
        return None
    mirror = HubMirror(settings, client, on_restart, logger)
    thread = SwitchMirrorThread(mirror, settings.ha_switch_poll_seconds, logger)
    thread.start()
    logger.info("Lights hub helpers: %s_*", settings.ha_hub_prefix)
    return mirror, thread
