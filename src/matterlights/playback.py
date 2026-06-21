"""Custom playback state and the pattern engine.

The sync loop runs in its own process and reads the control-state file by mtime,
exactly like it already does for the zone and preview-override files. This module
owns that file format plus the logic that turns a custom pattern into the discrete
light updates the sync loop should send.

Two playback modes exist:

* ``autonomous`` keeps the existing screen-driven behaviour.
* ``custom`` ignores the screen and drives every light from either a single static
  colour (``solid``) or a looping multi-colour ``pattern``.

A pattern is an ordered list of steps. Each step holds its colour for ``hold``
seconds and then the next step fades in over that next step's ``transition``
seconds. The fade is handled natively by Home Assistant / the bulb via the
``transition`` service parameter, so the engine only emits one update per
keyframe boundary rather than streaming intermediate frames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from matterlights.home_assistant import LightUpdate
from matterlights.screen import RgbColor


MODE_AUTONOMOUS = "autonomous"
MODE_CUSTOM = "custom"
VALID_MODES = frozenset({MODE_AUTONOMOUS, MODE_CUSTOM})

CUSTOM_SOLID = "solid"
CUSTOM_PATTERN = "pattern"
VALID_CUSTOM_TYPES = frozenset({CUSTOM_SOLID, CUSTOM_PATTERN})

# Each color is either an RGB color or a tunable-white color temperature.
COLOR_RGB = "rgb"
COLOR_WHITE = "white"
VALID_COLOR_MODES = frozenset({COLOR_RGB, COLOR_WHITE})

MAX_PATTERN_STEPS = 24
MAX_STEP_SECONDS = 3600.0
MIN_KELVIN = 1500
MAX_KELVIN = 8000
DEFAULT_KELVIN = 2700


@dataclass(frozen=True, slots=True)
class PatternStep:
    color: RgbColor
    hold: float
    transition: float
    mode: str = COLOR_RGB
    kelvin: int = DEFAULT_KELVIN


@dataclass(frozen=True, slots=True)
class CustomState:
    type: str = CUSTOM_SOLID
    brightness: int = 255
    solid_color: RgbColor = field(default_factory=lambda: RgbColor(255, 255, 255))
    solid_mode: str = COLOR_RGB
    solid_kelvin: int = DEFAULT_KELVIN
    pattern_steps: tuple[PatternStep, ...] = ()


@dataclass(frozen=True, slots=True)
class ControlState:
    mode: str = MODE_AUTONOMOUS
    custom: CustomState = field(default_factory=CustomState)


def default_control_state() -> ControlState:
    return ControlState(mode=MODE_AUTONOMOUS, custom=_default_custom_state())


def _default_custom_state() -> CustomState:
    return CustomState(
        type=CUSTOM_SOLID,
        brightness=255,
        solid_color=RgbColor(255, 255, 255),
        # The 10-second loop from the README: red holds 3s, fades to blue over 1s,
        # blue holds 4s, fades to yellow over 2s, then snaps back to red.
        pattern_steps=(
            PatternStep(RgbColor(255, 0, 0), hold=3.0, transition=0.0),
            PatternStep(RgbColor(0, 0, 255), hold=4.0, transition=1.0),
            PatternStep(RgbColor(255, 255, 0), hold=0.0, transition=2.0),
        ),
    )


# ---------------------------------------------------------------------------
# Pattern math
# ---------------------------------------------------------------------------


def pattern_cycle_seconds(steps: tuple[PatternStep, ...]) -> float:
    return sum(max(0.0, step.transition) + max(0.0, step.hold) for step in steps)


def pattern_events(steps: tuple[PatternStep, ...]) -> list[tuple[float, PatternStep]]:
    """Return ``(start_time, step)`` keyframes for one cycle.

    ``start_time`` is the offset within the cycle at which the fade *into* this
    step's colour begins; ``step.transition`` is that fade's duration. The colour
    is fully reached at ``start_time + transition`` and then held.
    """

    events: list[tuple[float, PatternStep]] = []
    cursor = 0.0
    for step in steps:
        events.append((cursor, step))
        cursor += max(0.0, step.transition) + max(0.0, step.hold)
    return events


def _event_index_at(events: list[tuple[float, PatternStep]], cycle_time: float) -> int:
    index = 0
    for candidate, (start_time, _) in enumerate(events):
        if cycle_time >= start_time:
            index = candidate
        else:
            break
    return index


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaybackCommand:
    updates: list[LightUpdate]
    transition_seconds: float
    target_key: tuple


class CustomPlayer:
    """Edge-triggered, per-light driver for custom playback.

    Each tick computes the colour every light should currently show. A light is
    sent an update only when its desired target differs from what was last
    optimistically applied to it, so a steady solid colour is sent once and a
    pattern emits one batch per keyframe. Lights whose update fails are
    un-confirmed by the sync loop via :meth:`mark_failed`, so only they are
    retried — a single slow bulb no longer restarts the pattern or re-floods the
    healthy lights, and retries snap (no transition) instead of stacking fades on
    a struggling bulb. The sync loop calls :meth:`reset` whenever the control file
    changes, the mode switches, or the display resumes so the next tick
    re-establishes every light.
    """

    __slots__ = ("_pattern_start", "_phase", "_confirmed", "_available")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._pattern_start: float | None = None
        self._phase: tuple | None = None
        self._confirmed: dict[str, tuple] = {}
        self._available: frozenset[str] = frozenset()

    def tick(self, custom: CustomState, available_entity_ids: list[str], now: float) -> PlaybackCommand | None:
        if not available_entity_ids:
            return None

        available = frozenset(available_entity_ids)
        if available != self._available:
            # A light that just came back may have lost its state while away.
            for entity_id in available - self._available:
                self._confirmed.pop(entity_id, None)
            self._available = available

        mode, color, kelvin, transition, phase = self._resolve(custom, now)
        target = _target_key(mode, color, kelvin, custom.brightness)
        if phase != self._phase:
            # New keyframe (or changed solid target): every light must change.
            self._phase = phase
            self._confirmed.clear()
            use_transition = transition
        else:
            # Steady state or retry: only un-confirmed lights, snapped instantly.
            use_transition = 0.0

        pending = [entity_id for entity_id in available_entity_ids if self._confirmed.get(entity_id) != target]
        if not pending:
            return None

        for entity_id in pending:
            self._confirmed[entity_id] = target  # optimistic; undone by mark_failed
        return PlaybackCommand(
            updates=_make_updates(pending, mode, color, kelvin, custom.brightness),
            transition_seconds=use_transition,
            target_key=target,
        )

    def mark_failed(self, target_key: tuple, failed_entity_ids) -> None:
        for entity_id in failed_entity_ids:
            if self._confirmed.get(entity_id) == target_key:
                del self._confirmed[entity_id]

    def _resolve(self, custom: CustomState, now: float):
        if custom.type == CUSTOM_PATTERN and custom.pattern_steps:
            steps = custom.pattern_steps
            cycle_seconds = pattern_cycle_seconds(steps)
            events = pattern_events(steps)
            if len(events) == 1 or cycle_seconds <= 0.0:
                step = events[0][1]
                phase = ("static", _target_key(step.mode, step.color, step.kelvin, custom.brightness))
                return step.mode, step.color, step.kelvin, 0.0, phase
            if self._pattern_start is None:
                self._pattern_start = now
            cycle_time = (now - self._pattern_start) % cycle_seconds
            index = _event_index_at(events, cycle_time)
            step = events[index][1]
            return step.mode, step.color, step.kelvin, max(0.0, step.transition), ("pattern", index)

        self._pattern_start = None
        key = _target_key(custom.solid_mode, custom.solid_color, custom.solid_kelvin, custom.brightness)
        return custom.solid_mode, custom.solid_color, custom.solid_kelvin, 0.0, ("static", key)


def _make_updates(
    entity_ids: list[str], mode: str, color: RgbColor, kelvin: int, brightness: int
) -> list[LightUpdate]:
    if mode == COLOR_WHITE:
        return [
            LightUpdate(entity_id, color=None, brightness=brightness, color_temp_kelvin=kelvin)
            for entity_id in entity_ids
        ]
    return [LightUpdate(entity_id, color=color, brightness=brightness) for entity_id in entity_ids]


def _target_key(mode: str, color: RgbColor, kelvin: int, brightness: int) -> tuple:
    if mode == COLOR_WHITE:
        return (COLOR_WHITE, kelvin, brightness)
    return (COLOR_RGB, tuple(color.as_list()), brightness)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def load_control_state(path: Path | None) -> ControlState:
    """Read the control state, returning the autonomous default on any problem."""

    if path is None or not path.exists():
        return default_control_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_control_state()
    try:
        return control_state_from_payload(payload)
    except (ValueError, TypeError, KeyError):
        return default_control_state()


def save_control_state(path: Path, state: ControlState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(control_state_to_payload(state), indent=2), encoding="utf-8")


def control_state_to_payload(state: ControlState) -> dict:
    custom = state.custom
    return {
        "version": 1,
        "mode": state.mode,
        "custom": {
            "type": custom.type,
            "brightness": custom.brightness,
            "solid": {
                "mode": custom.solid_mode,
                "color": custom.solid_color.as_list(),
                "kelvin": custom.solid_kelvin,
            },
            "pattern": {
                "steps": [
                    {
                        "mode": step.mode,
                        "color": step.color.as_list(),
                        "kelvin": step.kelvin,
                        "hold": round(step.hold, 3),
                        "transition": round(step.transition, 3),
                    }
                    for step in custom.pattern_steps
                ]
            },
        },
    }


def control_state_from_payload(payload: dict) -> ControlState:
    """Parse and validate a control-state payload, raising ``ValueError`` if invalid."""

    if not isinstance(payload, dict):
        raise ValueError("Control state must be an object")

    mode = str(payload.get("mode", MODE_AUTONOMOUS)).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")

    custom_payload = payload.get("custom") or {}
    if not isinstance(custom_payload, dict):
        raise ValueError("custom must be an object")

    custom_type = str(custom_payload.get("type", CUSTOM_SOLID)).strip().lower()
    if custom_type not in VALID_CUSTOM_TYPES:
        raise ValueError(f"custom.type must be one of: {', '.join(sorted(VALID_CUSTOM_TYPES))}")

    brightness = _parse_brightness(custom_payload.get("brightness", 255))

    solid_payload = custom_payload.get("solid") or {}
    solid_color = _parse_color((solid_payload or {}).get("color", [255, 255, 255]))
    solid_mode = _parse_color_mode((solid_payload or {}).get("mode", COLOR_RGB))
    solid_kelvin = _parse_kelvin((solid_payload or {}).get("kelvin", DEFAULT_KELVIN))

    pattern_payload = custom_payload.get("pattern") or {}
    raw_steps = (pattern_payload or {}).get("steps", []) or []
    if not isinstance(raw_steps, list):
        raise ValueError("custom.pattern.steps must be a list")
    if len(raw_steps) > MAX_PATTERN_STEPS:
        raise ValueError(f"A pattern may contain at most {MAX_PATTERN_STEPS} colors")

    pattern_steps = tuple(_parse_step(entry) for entry in raw_steps)
    if custom_type == CUSTOM_PATTERN and len(pattern_steps) < 1:
        raise ValueError("A pattern needs at least one color")

    return ControlState(
        mode=mode,
        custom=CustomState(
            type=custom_type,
            brightness=brightness,
            solid_color=solid_color,
            solid_mode=solid_mode,
            solid_kelvin=solid_kelvin,
            pattern_steps=pattern_steps,
        ),
    )


def _parse_step(entry: object) -> PatternStep:
    if not isinstance(entry, dict):
        raise ValueError("Each pattern step must be an object")
    return PatternStep(
        color=_parse_color(entry.get("color", [255, 255, 255])),
        hold=_parse_seconds(entry.get("hold", 0)),
        transition=_parse_seconds(entry.get("transition", 0)),
        mode=_parse_color_mode(entry.get("mode", COLOR_RGB)),
        kelvin=_parse_kelvin(entry.get("kelvin", DEFAULT_KELVIN)),
    )


def _parse_color_mode(value: object) -> str:
    return COLOR_WHITE if str(value).strip().lower() == COLOR_WHITE else COLOR_RGB


def _parse_kelvin(value: object) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("kelvin must be a number") from exc
    return max(MIN_KELVIN, min(MAX_KELVIN, number))


def _parse_color(value: object) -> RgbColor:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("color must be an array of 3 numbers")
    return RgbColor(*[_clamp_channel(channel) for channel in value])


def _parse_brightness(value: object) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("brightness must be a number") from exc
    return max(1, min(255, number))


def _parse_seconds(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("hold and transition must be numbers") from exc
    if number != number:  # NaN guard
        raise ValueError("hold and transition must be finite numbers")
    return max(0.0, min(MAX_STEP_SECONDS, number))


def _clamp_channel(value: object) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("color channels must be numbers") from exc
    return max(0, min(255, number))
