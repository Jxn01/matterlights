"""Ambience color engine for autonomous mode.

The older engines ask "what is the strongest color on screen?" and paint every
light with the answer: dominant-color extraction weighted by saturation squared,
so a small vivid patch can recolor the whole room even when the rest of the
frame is a muted brown. This engine asks a different question: "if the screen
were the only light source in the room, what would the room look like?"

It renders that answer with two physical light groups -- a *near* group beside
the screen and a *far* group deeper in the room -- in three stages:

1. Sample the whole frame into a coarse RGB histogram and cluster it into a
   small weighted palette. A color's weight is the light it actually
   contributes: its screen area times its brightness, with only a mild, bounded
   lift for saturation. A one-percent red spot therefore carries roughly one
   percent of the weight instead of winning a saturation-squared shouting
   match.

2. Apportion each group's bulb slots to palette colors by largest remainder.
   The far group reproduces the frame's overall balance; the near group
   carries its strongest components -- the screen's glow. Because bulbs in a
   group mix additively in the room, a frame that averages to a muddy brown is
   rendered as its live components (some amber, some teal) whose blend *is*
   that brown, rather than six bulbs all showing the same flat average.

3. Assign the resulting colors to concrete bulbs by matching against what each
   bulb showed last frame, so a 51/49 flip in cluster dominance does not make
   two bulbs trade colors every update.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from matterlights.screen import RgbColor, ScreenZone, ZoneSample, boost_saturation


_PALETTE_SIZE = 4
_KMEANS_ITERATIONS = 5
_BIN_SHIFT = 5
# Bins darker than this contribute darkness, not color: they dim the output via
# the frame's average brightness but never claim a bulb.
_MIN_PALETTE_LUMA = 10
_MIN_CLUSTER_WEIGHT = 0.02
# How far each palette component is pulled toward the overall mix, keeping the
# six bulbs reading as one scene instead of six unrelated colors.
_COHESION = 0.2
# Matches the active-pixel definition in screen.py's zone sampler.
_ACTIVE_CHANNEL_THRESHOLD = 24

_NEAR_ZONE = ScreenZone("ambience-near", 0.0, 0.0, 1.0, 1.0)
_FAR_ZONE = ScreenZone("ambience-far", 0.0, 0.0, 1.0, 1.0)

# Subtle hue/lightness/saturation offsets applied when one palette color owns
# several bulbs in the same group, so the repeat reads as depth rather than a
# wall of identical light.
_DUPLICATE_VARIANTS: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0, 1.0),
    (0.015, -0.04, 1.06),
    (-0.015, 0.04, 0.92),
    (0.03, -0.02, 0.9),
)


@dataclass(frozen=True, slots=True)
class PaletteColor:
    color: RgbColor
    weight: float


@dataclass(frozen=True, slots=True)
class AmbienceFrame:
    palette: tuple[PaletteColor, ...]
    mix: RgbColor
    average_brightness: int
    active_ratio: float


def resolve_near_entity_ids(
    configured_near_lights: list[str],
    primary_zone_names: list[str],
    entity_ids: list[str],
    light_zones: list[ScreenZone],
) -> list[str]:
    """Pick the bulbs that sit beside the screen.

    Preference order: the explicit AMBIENCE_NEAR_LIGHTS setting, then the
    shared-variant primary-zone mapping (so existing setups keep their meaning),
    then simply the first two configured entities.
    """

    if configured_near_lights:
        wanted = {entry.strip().lower() for entry in configured_near_lights if entry.strip()}
        matched = [entity_id for entity_id in entity_ids if entity_id.lower() in wanted]
        if matched:
            return matched

    zone_names = {name.strip().lower() for name in primary_zone_names if name.strip()}
    if zone_names and light_zones:
        matched = [
            entity_id
            for entity_id, zone in zip(entity_ids, light_zones)
            if zone.name.strip().lower() in zone_names
        ]
        if matched:
            return matched

    return list(entity_ids[:2])


def build_ambience_zone_samples(
    raw: bytes,
    width: int,
    height: int,
    sample_stride: int,
    color_boost: float,
    entity_ids: list[str],
    near_entity_ids: list[str],
    last_colors: dict[str, RgbColor],
) -> list[ZoneSample]:
    """Turn one frame into per-bulb samples, ordered to match ``entity_ids``."""

    frame = sample_frame(raw, width, height, sample_stride)

    near_set = set(near_entity_ids)
    near_group = [entity_id for entity_id in entity_ids if entity_id in near_set]
    far_group = [entity_id for entity_id in entity_ids if entity_id not in near_set]

    near_colors = [
        boost_saturation(color, color_boost)
        for color in _render_group_colors(frame.palette, frame.mix, len(near_group))
    ]
    far_colors = [
        boost_saturation(color, color_boost)
        for color in _render_group_colors(frame.palette, frame.mix, len(far_group))
    ]

    assigned = _assign_group(near_group, near_colors, last_colors)
    assigned.update(_assign_group(far_group, far_colors, last_colors))

    return [
        ZoneSample(
            zone=_NEAR_ZONE if entity_id in near_set else _FAR_ZONE,
            color=assigned[entity_id],
            average_brightness=frame.average_brightness,
            active_ratio=frame.active_ratio,
        )
        for entity_id in entity_ids
    ]


def sample_frame(raw: bytes, width: int, height: int, sample_stride: int) -> AmbienceFrame:
    """Distill one frame into a weighted palette plus global brightness stats."""

    sample_step = max(1, round(sample_stride ** 0.5))
    bins: dict[int, list[int]] = {}
    bins_get = bins.get
    brightness_total = 0
    active_count = 0
    pixel_count = 0
    red_total = 0
    green_total = 0
    blue_total = 0

    for y_pos in range(0, height, sample_step):
        row_offset = y_pos * width * 4
        for x_pos in range(0, width, sample_step):
            index = row_offset + x_pos * 4
            blue = raw[index]
            green = raw[index + 1]
            red = raw[index + 2]

            brightness_total += (54 * red + 183 * green + 19 * blue) >> 8
            red_total += red
            green_total += green
            blue_total += blue
            pixel_count += 1
            if red >= _ACTIVE_CHANNEL_THRESHOLD or green >= _ACTIVE_CHANNEL_THRESHOLD or blue >= _ACTIVE_CHANNEL_THRESHOLD:
                active_count += 1

            key = ((red >> _BIN_SHIFT) << 6) | ((green >> _BIN_SHIFT) << 3) | (blue >> _BIN_SHIFT)
            entry = bins_get(key)
            if entry is None:
                bins[key] = [1, red, green, blue]
            else:
                entry[0] += 1
                entry[1] += red
                entry[2] += green
                entry[3] += blue

    if pixel_count == 0:
        return AmbienceFrame((), RgbColor(0, 0, 0), 0, 0.0)

    average_brightness = brightness_total // pixel_count
    active_ratio = active_count / pixel_count
    average_color = RgbColor(
        red=red_total // pixel_count,
        green=green_total // pixel_count,
        blue=blue_total // pixel_count,
    )

    candidates: list[tuple[float, int, int, int]] = []
    for count, red_sum, green_sum, blue_sum in bins.values():
        red = red_sum // count
        green = green_sum // count
        blue = blue_sum // count
        luma = (54 * red + 183 * green + 19 * blue) >> 8
        if luma < _MIN_PALETTE_LUMA:
            continue
        brightest = max(red, green, blue)
        saturation_ratio = (brightest - min(red, green, blue)) / brightest if brightest else 0.0
        # Area times brightness, with a bounded (at most 2x) lift for saturation:
        # colored light registers a little more than neutral light of equal
        # luminance, but can never out-shout a large area the way the old
        # saturation-squared weighting could.
        mass = count * luma * (1.0 + saturation_ratio)
        candidates.append((mass, red, green, blue))

    palette = _cluster_palette(candidates, _PALETTE_SIZE)
    if palette:
        mix = RgbColor(
            red=_clamp_channel(sum(entry.color.red * entry.weight for entry in palette)),
            green=_clamp_channel(sum(entry.color.green * entry.weight for entry in palette)),
            blue=_clamp_channel(sum(entry.color.blue * entry.weight for entry in palette)),
        )
    else:
        mix = average_color

    return AmbienceFrame(tuple(palette), mix, average_brightness, active_ratio)


def _cluster_palette(
    candidates: list[tuple[float, int, int, int]],
    palette_size: int,
) -> list[PaletteColor]:
    """Deterministic weighted k-means over histogram bins (not pixels)."""

    if not candidates:
        return []

    ordered = sorted(candidates, reverse=True)
    seeds = [ordered[0]]
    while len(seeds) < min(palette_size, len(ordered)):
        best_candidate = None
        best_score = 0.0
        for candidate in ordered:
            nearest = min(_bin_distance_squared(candidate, seed) for seed in seeds)
            score = candidate[0] * nearest
            if score > best_score:
                best_score = score
                best_candidate = candidate
        if best_candidate is None:
            break
        seeds.append(best_candidate)

    centers = [(float(seed[1]), float(seed[2]), float(seed[3])) for seed in seeds]
    for _ in range(_KMEANS_ITERATIONS):
        sums = [[0.0, 0.0, 0.0, 0.0] for _ in centers]
        for mass, red, green, blue in ordered:
            best_index = _nearest_center(centers, red, green, blue)
            bucket = sums[best_index]
            bucket[0] += mass
            bucket[1] += mass * red
            bucket[2] += mass * green
            bucket[3] += mass * blue
        moved = False
        for index, bucket in enumerate(sums):
            if bucket[0] <= 0:
                continue
            new_center = (bucket[1] / bucket[0], bucket[2] / bucket[0], bucket[3] / bucket[0])
            if new_center != centers[index]:
                moved = True
                centers[index] = new_center
        if not moved:
            break

    totals = [0.0] * len(centers)
    for mass, red, green, blue in ordered:
        totals[_nearest_center(centers, red, green, blue)] += mass

    total_mass = sum(totals)
    if total_mass <= 0:
        return []

    clusters = sorted(
        (
            (mass / total_mass, centers[index])
            for index, mass in enumerate(totals)
            if mass > 0
        ),
        reverse=True,
    )
    kept = [entry for entry in clusters if entry[0] >= _MIN_CLUSTER_WEIGHT] or clusters[:1]
    kept_mass = sum(weight for weight, _ in kept)
    return [
        PaletteColor(
            color=RgbColor(
                red=_clamp_channel(center[0]),
                green=_clamp_channel(center[1]),
                blue=_clamp_channel(center[2]),
            ),
            weight=weight / kept_mass,
        )
        for weight, center in kept
    ]


def _render_group_colors(
    palette: tuple[PaletteColor, ...] | list[PaletteColor],
    mix: RgbColor,
    slot_count: int,
) -> list[RgbColor]:
    if slot_count <= 0:
        return []
    if not palette:
        return [mix] * slot_count

    counts = _apportion_slots([entry.weight for entry in palette], slot_count)
    colors: list[RgbColor] = []
    for entry, count in zip(palette, counts):
        base = _blend(entry.color, mix, _COHESION)
        for duplicate_index in range(count):
            colors.append(_apply_variant(base, duplicate_index))
    return colors


def _apportion_slots(weights: list[float], slot_count: int) -> list[int]:
    """Largest-remainder apportionment; ties go to the more dominant color."""

    quotas = [weight * slot_count for weight in weights]
    counts = [int(quota) for quota in quotas]
    # Quantize remainders so binary float dust (0.6 * 4 == 2.3999...) cannot
    # decide what should be a tie; genuine ties then fall to the dominant color.
    order = sorted(
        range(len(weights)),
        key=lambda index: (round(quotas[index] - counts[index], 9), -index),
        reverse=True,
    )
    assigned = sum(counts)
    cursor = 0
    while assigned < slot_count and order:
        counts[order[cursor % len(order)]] += 1
        cursor += 1
        assigned += 1
    return counts


def _assign_group(
    entity_ids: list[str],
    colors: list[RgbColor],
    last_colors: dict[str, RgbColor],
) -> dict[str, RgbColor]:
    """Match slot colors to bulbs, keeping each bulb near what it last showed."""

    pending = list(range(len(colors)))
    unassigned = list(entity_ids)
    result: dict[str, RgbColor] = {}

    while pending:
        best: tuple[int, str, int] | None = None
        for entity_id in unassigned:
            previous = last_colors.get(entity_id)
            if previous is None:
                continue
            for slot in pending:
                distance = previous.distance(colors[slot])
                if best is None or distance < best[0]:
                    best = (distance, entity_id, slot)
        if best is None:
            break
        _, entity_id, slot = best
        result[entity_id] = colors[slot]
        unassigned.remove(entity_id)
        pending.remove(slot)

    for entity_id, slot in zip(unassigned, pending):
        result[entity_id] = colors[slot]
    return result


def _apply_variant(color: RgbColor, duplicate_index: int) -> RgbColor:
    if duplicate_index == 0 or color.max_channel() == 0:
        return color
    hue_shift, lightness_shift, saturation_scale = _DUPLICATE_VARIANTS[
        duplicate_index % len(_DUPLICATE_VARIANTS)
    ]
    hue, lightness, saturation = colorsys.rgb_to_hls(
        color.red / 255, color.green / 255, color.blue / 255
    )
    hue = (hue + hue_shift) % 1.0
    lightness = min(1.0, max(0.0, lightness + lightness_shift))
    saturation = min(1.0, max(0.0, saturation * saturation_scale))
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return RgbColor(
        red=_clamp_channel(red * 255),
        green=_clamp_channel(green * 255),
        blue=_clamp_channel(blue * 255),
    )


def _blend(color: RgbColor, toward: RgbColor, fraction: float) -> RgbColor:
    if fraction <= 0:
        return color
    return RgbColor(
        red=_clamp_channel(color.red + (toward.red - color.red) * fraction),
        green=_clamp_channel(color.green + (toward.green - color.green) * fraction),
        blue=_clamp_channel(color.blue + (toward.blue - color.blue) * fraction),
    )


def _bin_distance_squared(a: tuple[float, int, int, int], b: tuple[float, int, int, int]) -> int:
    red_delta = a[1] - b[1]
    green_delta = a[2] - b[2]
    blue_delta = a[3] - b[3]
    return red_delta * red_delta + green_delta * green_delta + blue_delta * blue_delta


def _nearest_center(centers: list[tuple[float, float, float]], red: int, green: int, blue: int) -> int:
    best_index = 0
    best_distance = None
    for index, (center_red, center_green, center_blue) in enumerate(centers):
        red_delta = red - center_red
        green_delta = green - center_green
        blue_delta = blue - center_blue
        distance = red_delta * red_delta + green_delta * green_delta + blue_delta * blue_delta
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def _clamp_channel(value: float) -> int:
    return max(0, min(255, round(value)))
