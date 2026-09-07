from __future__ import annotations

from ctypes import windll
from dataclasses import dataclass
import json
import logging
import re
import threading
from pathlib import Path

from mss import MSS, tools

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RgbColor:
    red: int
    green: int
    blue: int

    def as_list(self) -> list[int]:
        return [self.red, self.green, self.blue]

    def max_channel(self) -> int:
        return max(self.red, self.green, self.blue)

    def distance(self, other: "RgbColor") -> int:
        return abs(self.red - other.red) + abs(self.green - other.green) + abs(self.blue - other.blue)


@dataclass(frozen=True, slots=True)
class ScreenZone:
    name: str
    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True, slots=True)
class ZoneSample:
    zone: ScreenZone
    color: RgbColor
    average_brightness: int
    active_ratio: float

    def effective_brightness(self, floor: int, full_frame_active_ratio: float = 0.35) -> int:
        activity_scale = min(1.0, self.active_ratio / full_frame_active_ratio)
        activity_brightness = self.color.max_channel() * activity_scale
        return max(floor, _clamp_channel(max(self.average_brightness, activity_brightness)))

    def should_turn_off(self, dark_threshold: int, dark_active_ratio_threshold: float) -> bool:
        return self.average_brightness <= dark_threshold and self.active_ratio <= dark_active_ratio_threshold


_ZONE_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "top-left": (0.0, 0.0, 0.38, 0.35),
    "top-center": (0.25, 0.0, 0.75, 0.28),
    "top-right": (0.62, 0.0, 1.0, 0.35),
    "right-top": (0.72, 0.0, 1.0, 0.5),
    "right-center": (0.72, 0.18, 1.0, 0.82),
    "right-bottom": (0.72, 0.5, 1.0, 1.0),
    "bottom-right": (0.62, 0.65, 1.0, 1.0),
    "bottom-center": (0.25, 0.72, 0.75, 1.0),
    "bottom-left": (0.0, 0.65, 0.38, 1.0),
    "left-bottom": (0.0, 0.5, 0.28, 1.0),
    "left-center": (0.0, 0.18, 0.28, 0.82),
    "left-top": (0.0, 0.0, 0.28, 0.5),
    "center": (0.25, 0.2, 0.75, 0.8),
}

# ---------------------------------------------------------------------------
# Capture backend: DXGI Desktop Duplication (dxcam), falling back to mss/GDI.
#
# WHY THIS EXISTS. mss captures by BitBlt-ing from the screen DC. Sustained
# full-screen GDI readback on this rig (RTX 5090, five displays, 4K HDR primary)
# makes the compositor mis-draw the mouse cursor at the capture origin -- it
# visibly snaps to the top-left of the captured monitor several times a second,
# and the text caret in other applications flickers with it. Measured 2026-09-08:
# with MatterLights stopped, a bare capture loop reproduced it on its own after
# roughly two minutes of sustained grabbing, and it stopped the moment capture
# stopped. GetCursorPos never moves during this, so nothing is moving the pointer
# -- it is purely a compositing artifact of the GDI screen readback.
#
# Desktop Duplication avoids the problem class entirely: it receives frames from
# DXGI instead of reading back the screen DC, and is substantially faster.
#
# Two dxcam behaviours callers must not trip over:
#   * grab() returns None when no NEW frame has arrived since the last call. On a
#     static screen that is normal, not an error, so the last good frame is kept.
#   * A camera is bound to ONE output, so the "all monitors" target cannot use it.
# Anything unsupported falls back to mss, so behaviour never regresses.
# ---------------------------------------------------------------------------

_DXCAM_LOCK = threading.Lock()
_DXCAM_CAMERAS: dict[tuple[int, int], object] = {}
_DXCAM_LAST_FRAME: dict[tuple[int, int], tuple[bytes, int, int]] = {}
_DXCAM_OUTPUTS: list[dict[str, int | bool]] | None = None
_DXCAM_UNAVAILABLE = False

_OUTPUT_INFO_RE = re.compile(
    r"Device\[(\d+)\]\s*Output\[(\d+)\]:\s*Res:\((\d+),\s*(\d+)\)\s*Rot:(\d+)\s*Primary:(True|False)"
)


def _dxcam_outputs() -> list[dict[str, int | bool]]:
    """Parse dxcam's output table into something matchable against mss monitors.

    dxcam reports resolution/rotation/primary but not desktop position, so the
    match below is on size plus the primary flag.
    """

    global _DXCAM_OUTPUTS, _DXCAM_UNAVAILABLE
    if _DXCAM_OUTPUTS is not None:
        return _DXCAM_OUTPUTS
    try:
        import dxcam  # imported lazily so mss-only environments still work

        outputs: list[dict[str, int | bool]] = []
        for match in _OUTPUT_INFO_RE.finditer(dxcam.output_info()):
            device_idx, output_idx, width, height, _rotation, primary = match.groups()
            outputs.append(
                {
                    "device_idx": int(device_idx),
                    "output_idx": int(output_idx),
                    "width": int(width),
                    "height": int(height),
                    "primary": primary == "True",
                }
            )
        _DXCAM_OUTPUTS = outputs
        LOGGER.info("Desktop Duplication available: %d output(s)", len(outputs))
    except Exception as error:  # noqa: BLE001 - any failure means "use mss"
        _DXCAM_UNAVAILABLE = True
        _DXCAM_OUTPUTS = []
        LOGGER.warning("Desktop Duplication unavailable (%s); using GDI capture", error)
    return _DXCAM_OUTPUTS


def _dxcam_output_for_region(region: dict) -> tuple[int, int] | None:
    """Find the single dxcam output that exactly covers ``region``.

    Returns None when the region spans several screens (the ``all`` target) or
    when two outputs share a resolution and cannot be told apart -- both cases
    fall back to mss rather than risk capturing the wrong screen.
    """

    outputs = _dxcam_outputs()
    if not outputs:
        return None

    width = int(region.get("width", 0))
    height = int(region.get("height", 0))
    candidates = [o for o in outputs if o["width"] == width and o["height"] == height]
    if not candidates:
        return None

    if len(candidates) > 1:
        primary_candidates = [o for o in candidates if o["primary"]]
        if region.get("is_primary") and len(primary_candidates) == 1:
            candidates = primary_candidates
        else:
            # Two identical screens (the pair of portrait 4K panels here) cannot be
            # distinguished from resolution alone, and dxcam exposes no position.
            return None

    chosen = candidates[0]
    return int(chosen["device_idx"]), int(chosen["output_idx"])


def _dxcam_capture(region: dict) -> tuple[bytes, int, int] | None:
    """Grab ``region`` via Desktop Duplication, or None if that is not possible."""

    if _DXCAM_UNAVAILABLE:
        return None
    key = _dxcam_output_for_region(region)
    if key is None:
        return None

    try:
        import dxcam

        with _DXCAM_LOCK:
            camera = _DXCAM_CAMERAS.get(key)
            if camera is None:
                device_idx, output_idx = key
                # BGRA matches the byte order the zone sampler expects, so the
                # frame can be handed on without a colour conversion.
                camera = dxcam.create(
                    device_idx=device_idx, output_idx=output_idx, output_color="BGRA"
                )
                if camera is None:
                    return None
                _DXCAM_CAMERAS[key] = camera

            frame = camera.grab()
            if frame is None:
                # No new frame since the last call: the screen has not changed.
                # Reuse the previous one rather than reporting a black screen,
                # which would make the lights blink off on a static desktop.
                return _DXCAM_LAST_FRAME.get(key)

            height, width = frame.shape[0], frame.shape[1]
            raw = frame.tobytes()
            _DXCAM_LAST_FRAME[key] = (raw, width, height)
            return raw, width, height
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Desktop Duplication capture failed (%s); using GDI capture", error)
        with _DXCAM_LOCK:
            _DXCAM_CAMERAS.pop(key, None)
        return None


def _grab_raw(sct: MSS, capture_target: str) -> tuple[bytes, int, int]:
    """Return one frame as raw BGRA bytes, preferring Desktop Duplication."""

    region = _capture_region(sct, capture_target)
    captured = _dxcam_capture(region)
    if captured is not None:
        return captured
    screenshot = sct.grab(region)
    return screenshot.raw, screenshot.width, screenshot.height


_DOMINANT_COLOR_BUCKET_SIZE = 24
_VIVID_PIXEL_MIN_BRIGHTNESS = 28
_VIVID_PIXEL_MIN_SATURATION = 28
_VIVID_PIXEL_MIN_SATURATION_RATIO = 0.22
_AMBIENT_MIN_DOMINANT_SUPPORT = 0.28
_AMBIENT_BLEND_DOMINANT_WEIGHT = 0.4
_AMBIENT_NEUTRAL_SATURATION = 18


def capture_average_color(
    sample_stride: int,
    color_boost: float = 1.15,
    capture_target: str = "primary",
) -> RgbColor:
    return capture_zone_colors(
        sample_stride,
        color_boost,
        capture_target,
        [ScreenZone("full", *_ZONE_PRESETS["full"])],
    )[0]


def list_monitors() -> list[dict[str, int]]:
    """Describe every attached screen so the UI can offer a real choice.

    Index 0 is mss's virtual bounding box covering all screens (the ``all``
    capture target); indexes 1..N are the physical monitors.
    """

    with MSS() as sct:
        monitors = [dict(monitor) for monitor in sct.monitors]
    return [
        {
            "index": index,
            "left": int(monitor.get("left", 0)),
            "top": int(monitor.get("top", 0)),
            "width": int(monitor.get("width", 0)),
            "height": int(monitor.get("height", 0)),
        }
        for index, monitor in enumerate(monitors)
    ]


def capture_screen_png(capture_target: str = "primary") -> tuple[bytes, int, int]:
    with MSS() as sct:
        screenshot = _grab_screenshot(sct, capture_target)
    return tools.to_png(screenshot.rgb, screenshot.size), screenshot.width, screenshot.height


def capture_screen_thumbnail_png(capture_target: str = "primary", max_width: int = 960) -> tuple[bytes, int, int]:
    """Capture a screen and shrink it by pixel striding before encoding.

    A 4K frame encodes to ~8 MB of PNG and the whole virtual desktop to ~40 MB,
    which is far too heavy to hand to a browser for a preview. Nearest-neighbour
    striding keeps this dependency-free (no Pillow) and is plenty for a thumbnail.
    """

    with MSS() as sct:
        screenshot = _grab_screenshot(sct, capture_target)

    width = screenshot.width
    height = screenshot.height
    step = max(1, -(-width // max(1, max_width)))
    if step == 1:
        return tools.to_png(screenshot.rgb, screenshot.size), width, height

    source = screenshot.rgb
    row_stride = width * 3
    columns = range(0, width, step)
    rows = bytearray()
    for y_pos in range(0, height, step):
        row_start = y_pos * row_stride
        row = source[row_start:row_start + row_stride]
        rows += b"".join(row[x_pos * 3:x_pos * 3 + 3] for x_pos in columns)

    thumb_width = len(columns)
    thumb_height = len(range(0, height, step))
    return tools.to_png(bytes(rows), (thumb_width, thumb_height)), thumb_width, thumb_height


def capture_zone_colors(
    sample_stride: int,
    color_boost: float = 1.15,
    capture_target: str = "primary",
    zones: list[ScreenZone] | None = None,
) -> list[RgbColor]:
    return [sample.color for sample in capture_zone_samples(sample_stride, color_boost, capture_target, zones)]


def capture_zone_samples(
    sample_stride: int,
    color_boost: float = 1.15,
    capture_target: str = "primary",
    zones: list[ScreenZone] | None = None,
) -> list[ZoneSample]:
    resolved_zones = zones or [ScreenZone("full", *_ZONE_PRESETS["full"])]
    with MSS() as sct:
        raw, width, height = _grab_raw(sct, capture_target)
    return sample_zone_samples_from_screenshot(
        raw,
        width,
        height,
        sample_stride,
        color_boost,
        resolved_zones,
    )


def capture_raw_with_session(sct: MSS, capture_target: str = "primary") -> tuple[bytes, int, int]:
    """Grab one frame and return its raw BGRA buffer plus dimensions.

    This is the hot path: the sync loop calls it several times a second forever,
    so it goes through Desktop Duplication when possible. See the backend notes
    above for why sustained GDI capture is avoided.
    """

    return _grab_raw(sct, capture_target)


def capture_zone_samples_with_session(
    sct: MSS,
    sample_stride: int,
    color_boost: float = 1.15,
    capture_target: str = "primary",
    zones: list[ScreenZone] | None = None,
) -> list[ZoneSample]:
    resolved_zones = zones or [ScreenZone("full", *_ZONE_PRESETS["full"])]
    raw, width, height = _grab_raw(sct, capture_target)
    return sample_zone_samples_from_screenshot(
        raw,
        width,
        height,
        sample_stride,
        color_boost,
        resolved_zones,
    )


def sample_zone_samples_from_screenshot(
    raw: bytes,
    width: int,
    height: int,
    sample_stride: int,
    color_boost: float,
    zones: list[ScreenZone],
) -> list[ZoneSample]:
    return _sample_zone_samples(
        raw,
        width,
        height,
        sample_stride,
        color_boost,
        zones,
    )


def load_configured_light_zones(
    zone_names: list[str],
    entity_ids: list[str],
    zone_file: Path | None,
) -> list[ScreenZone]:
    if zone_file is not None and zone_file.exists():
        return _load_light_zones_from_file(zone_file, entity_ids)
    return resolve_light_zones(zone_names, len(entity_ids))


def save_light_zones(zone_file: Path, entity_ids: list[str], zones: list[ScreenZone]) -> None:
    if len(entity_ids) != len(zones):
        raise ValueError("One zone is required for each configured light entity")

    payload = {
        "version": 1,
        "zones": [
            {
                "entity_id": entity_id,
                "name": zone.name,
                "left": round(zone.left, 6),
                "top": round(zone.top, 6),
                "right": round(zone.right, 6),
                "bottom": round(zone.bottom, 6),
            }
            for entity_id, zone in zip(entity_ids, zones)
        ],
    }
    zone_file.parent.mkdir(parents=True, exist_ok=True)
    zone_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def resolve_light_zones(zone_names: list[str], light_count: int) -> list[ScreenZone]:
    resolved_names = zone_names or default_light_zone_layout(light_count)
    zones: list[ScreenZone] = []
    for zone_name in resolved_names:
        normalized_name = zone_name.strip().lower()
        if normalized_name not in _ZONE_PRESETS:
            valid_zone_names = ", ".join(sorted(_ZONE_PRESETS))
            raise ValueError(f"Unknown LIGHT_ZONE_LAYOUT entry '{zone_name}'. Valid zones: {valid_zone_names}")
        zones.append(ScreenZone(normalized_name, *_ZONE_PRESETS[normalized_name]))
    return zones


def default_light_zone_layout(light_count: int) -> list[str]:
    default_layouts = {
        1: ["full"],
        2: ["left-center", "right-center"],
        3: ["left-center", "top-center", "right-center"],
        4: ["top-left", "top-right", "bottom-right", "bottom-left"],
        5: ["left-center", "top-left", "top-right", "right-center", "bottom-center"],
        6: ["top-left", "top-center", "top-right", "bottom-right", "bottom-center", "bottom-left"],
    }
    if light_count in default_layouts:
        return default_layouts[light_count]

    perimeter_order = [
        "top-left",
        "top-center",
        "top-right",
        "right-center",
        "bottom-right",
        "bottom-center",
        "bottom-left",
        "left-center",
    ]
    return [perimeter_order[index % len(perimeter_order)] for index in range(light_count)]


def brightness_for_color(color: RgbColor, floor: int) -> int:
    return max(floor, color.red, color.green, color.blue)


def _primary_monitor_region(sct: MSS | None = None) -> dict[str, int]:
    """Describe the primary screen, in PHYSICAL pixels.

    ``GetSystemMetrics(SM_CXSCREEN)`` returns DPI-VIRTUALISED pixels in a
    DPI-unaware process: on this machine's 3840x2160 panel at 125% scaling it
    reports 3072x1728. That still captures the whole screen (scaled), so zone
    sampling was unaffected -- but it never equals a real monitor rectangle,
    which stopped Desktop Duplication from recognising the region and silently
    forced the slow GDI path. mss reports physical pixels, so prefer its monitor
    table and keep GetSystemMetrics purely as a fallback.
    """

    if sct is not None:
        for monitor in sct.monitors[1:]:
            if monitor.get("is_primary"):
                return dict(monitor)
        if len(sct.monitors) > 1:
            return dict(sct.monitors[1])
    return {
        "left": 0,
        "top": 0,
        "width": int(windll.user32.GetSystemMetrics(0)),
        "height": int(windll.user32.GetSystemMetrics(1)),
    }


def _capture_region(sct: MSS, capture_target: str) -> dict[str, int]:
    normalized_target = capture_target.strip().lower()
    if normalized_target == "primary":
        return _primary_monitor_region(sct)
    if normalized_target == "all":
        return dict(sct.monitors[0])

    monitor_index = int(normalized_target)
    if monitor_index >= len(sct.monitors):
        raise ValueError(
            f"Monitor index {monitor_index} is not available "
            f"({len(sct.monitors) - 1} screen(s) attached)"
        )
    return dict(sct.monitors[monitor_index])


def _grab_screenshot(sct: MSS, capture_target: str):
    monitor = _capture_region(sct, capture_target)
    return sct.grab(monitor)


def _sample_zone_samples(
    raw: bytes,
    width: int,
    height: int,
    sample_stride: int,
    color_boost: float,
    zones: list[ScreenZone],
) -> list[ZoneSample]:
    return [
        _sample_zone(raw, width, height, sample_stride, color_boost, zone)
        for zone in zones
    ]


def _sample_zone(
    raw: bytes,
    width: int,
    height: int,
    sample_stride: int,
    color_boost: float,
    zone: ScreenZone,
) -> ZoneSample:
    sample_step = max(1, round(sample_stride ** 0.5))
    left, top, right, bottom = _zone_pixel_bounds(width, height, zone)
    red_total = 0
    green_total = 0
    blue_total = 0
    brightness_total = 0
    dominant_color_buckets: dict[tuple[int, int, int], list[int]] = {}
    active_pixel_count = 0
    pixel_count = 0
    vivid_pixel_count = 0

    for y_pos in range(top, bottom, sample_step):
        row_offset = y_pos * width * 4
        for x_pos in range(left, right, sample_step):
            index = row_offset + x_pos * 4
            blue = raw[index]
            green = raw[index + 1]
            red = raw[index + 2]

            blue_total += blue
            green_total += green
            red_total += red
            brightness_total += _perceived_brightness(red, green, blue)
            pixel_count += 1

            brightest_channel = max(red, green, blue)
            saturation = brightest_channel - min(red, green, blue)
            if brightest_channel >= 24:
                active_pixel_count += 1
            if brightest_channel == 0:
                continue

            saturation_ratio = saturation / brightest_channel
            if (
                brightest_channel < _VIVID_PIXEL_MIN_BRIGHTNESS
                or saturation < _VIVID_PIXEL_MIN_SATURATION
                or saturation_ratio < _VIVID_PIXEL_MIN_SATURATION_RATIO
            ):
                continue

            weight = brightest_channel * saturation * saturation
            bucket_key = _dominant_color_bucket(red, green, blue, brightest_channel)
            bucket_totals = dominant_color_buckets.get(bucket_key)
            if bucket_totals is None:
                dominant_color_buckets[bucket_key] = [weight, red * weight, green * weight, blue * weight, 1]
            else:
                bucket_totals[0] += weight
                bucket_totals[1] += red * weight
                bucket_totals[2] += green * weight
                bucket_totals[3] += blue * weight
                bucket_totals[4] += 1
            vivid_pixel_count += 1

    if pixel_count == 0:
        return ZoneSample(zone=zone, color=RgbColor(0, 0, 0), average_brightness=0, active_ratio=0.0)

    average_brightness = brightness_total // pixel_count
    active_ratio = active_pixel_count / pixel_count
    average_color = RgbColor(
        red=red_total // pixel_count,
        green=green_total // pixel_count,
        blue=blue_total // pixel_count,
    )

    if not dominant_color_buckets:
        if average_brightness <= 4:
            color = RgbColor(0, 0, 0)
        else:
            color = average_color
    else:
        dominant_bucket = max(dominant_color_buckets.values(), key=lambda totals: totals[0])
        dominant_weight = dominant_bucket[0]
        color = RgbColor(
            red=dominant_bucket[1] // dominant_weight,
            green=dominant_bucket[2] // dominant_weight,
            blue=dominant_bucket[3] // dominant_weight,
        )
        if zone.name.startswith("ambient-"):
            dominant_support = dominant_bucket[4] / vivid_pixel_count if vivid_pixel_count else 0.0
            color = _ambient_zone_color(color, average_color, dominant_support)

    return ZoneSample(
        zone=zone,
        color=boost_saturation(color, color_boost),
        average_brightness=average_brightness,
        active_ratio=active_ratio,
    )


def _zone_pixel_bounds(width: int, height: int, zone: ScreenZone) -> tuple[int, int, int, int]:
    left = max(0, min(width - 1, int(zone.left * width)))
    top = max(0, min(height - 1, int(zone.top * height)))
    right = max(left + 1, min(width, int(zone.right * width)))
    bottom = max(top + 1, min(height, int(zone.bottom * height)))
    return left, top, right, bottom


def _perceived_brightness(red: int, green: int, blue: int) -> int:
    return (54 * red + 183 * green + 19 * blue) // 256


def _ambient_zone_color(dominant_color: RgbColor, average_color: RgbColor, dominant_support: float) -> RgbColor:
    average_saturation = _color_saturation(average_color)
    if dominant_support < _AMBIENT_MIN_DOMINANT_SUPPORT:
        if average_saturation < _AMBIENT_NEUTRAL_SATURATION:
            return average_color
        return _blend_colors(average_color, dominant_color, _AMBIENT_BLEND_DOMINANT_WEIGHT)
    return dominant_color


def _blend_colors(base_color: RgbColor, accent_color: RgbColor, accent_weight: float) -> RgbColor:
    base_weight = 1.0 - accent_weight
    return RgbColor(
        red=_clamp_channel(base_color.red * base_weight + accent_color.red * accent_weight),
        green=_clamp_channel(base_color.green * base_weight + accent_color.green * accent_weight),
        blue=_clamp_channel(base_color.blue * base_weight + accent_color.blue * accent_weight),
    )


def _color_saturation(color: RgbColor) -> int:
    return color.max_channel() - min(color.red, color.green, color.blue)


def _dominant_color_bucket(red: int, green: int, blue: int, brightest_channel: int) -> tuple[int, int, int]:
    normalized_red = (red * 255) // brightest_channel
    normalized_green = (green * 255) // brightest_channel
    normalized_blue = (blue * 255) // brightest_channel
    return (
        normalized_red // _DOMINANT_COLOR_BUCKET_SIZE,
        normalized_green // _DOMINANT_COLOR_BUCKET_SIZE,
        normalized_blue // _DOMINANT_COLOR_BUCKET_SIZE,
    )


def _load_light_zones_from_file(zone_file: Path, entity_ids: list[str]) -> list[ScreenZone]:
    data = json.loads(zone_file.read_text(encoding="utf-8"))
    zone_map: dict[str, ScreenZone] = {}
    for entry in data.get("zones", []):
        entity_id = str(entry["entity_id"]).strip()
        zone = ScreenZone(
            name=str(entry.get("name", entity_id)).strip() or entity_id,
            left=float(entry["left"]),
            top=float(entry["top"]),
            right=float(entry["right"]),
            bottom=float(entry["bottom"]),
        )
        _validate_zone(zone)
        zone_map[entity_id] = zone

    missing_entity_ids = [entity_id for entity_id in entity_ids if entity_id not in zone_map]
    if missing_entity_ids:
        raise ValueError(
            "LIGHT_ZONE_FILE is missing zones for: " + ", ".join(missing_entity_ids)
        )
    return [zone_map[entity_id] for entity_id in entity_ids]


def _validate_zone(zone: ScreenZone) -> None:
    if not 0 <= zone.left < zone.right <= 1:
        raise ValueError(f"Invalid horizontal bounds for zone {zone.name}")
    if not 0 <= zone.top < zone.bottom <= 1:
        raise ValueError(f"Invalid vertical bounds for zone {zone.name}")


def boost_saturation(color: RgbColor, factor: float = 1.45) -> RgbColor:
    if factor == 1.0:
        return color
    midpoint = (color.red + color.green + color.blue) / 3
    return RgbColor(
        red=_clamp_channel(midpoint + (color.red - midpoint) * factor),
        green=_clamp_channel(midpoint + (color.green - midpoint) * factor),
        blue=_clamp_channel(midpoint + (color.blue - midpoint) * factor),
    )


def _clamp_channel(value: float) -> int:
    return max(0, min(255, round(value)))
