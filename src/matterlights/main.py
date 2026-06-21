from __future__ import annotations

import colorsys
from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
import time

from mss import MSS

from matterlights.config import load_settings
from matterlights.display_power import start_display_monitor
from matterlights.home_assistant import HomeAssistantClient, LightUpdate
from matterlights.playback import MODE_CUSTOM, CustomPlayer, load_control_state
from matterlights.process_lock import acquire_sync_singleton
from matterlights.preview import load_preview_overrides
from matterlights.screen import (
    RgbColor,
    ScreenZone,
    ZoneSample,
    capture_zone_samples_with_session,
    load_configured_light_zones,
)


LOGGER = logging.getLogger("matterlights")
FULL_SCREEN_ZONE = ScreenZone("full", 0.0, 0.0, 1.0, 1.0)
AMBIENT_EDGE_CAPTURE_ZONES: tuple[ScreenZone, ...] = (
    ScreenZone("ambient-top", 0.0, 0.0, 1.0, 0.18),
    ScreenZone("ambient-left", 0.0, 0.12, 0.16, 0.82),
    ScreenZone("ambient-right", 0.84, 0.12, 1.0, 0.82),
    ScreenZone("ambient-bottom-left", 0.0, 0.84, 0.36, 1.0),
    ScreenZone("ambient-bottom-right", 0.64, 0.84, 1.0, 1.0),
)
AMBIENT_EDGE_ZONE_WEIGHTS: dict[str, float] = {
    "ambient-top": 1.0,
    "ambient-left": 0.9,
    "ambient-right": 0.9,
    "ambient-bottom-left": 0.35,
    "ambient-bottom-right": 0.35,
}
SECONDARY_COLOR_VARIANTS: tuple[tuple[float, float, float], ...] = (
    (-0.018, -0.06, 1.12),
    (-0.006, 0.04, 0.94),
    (0.014, -0.03, 1.08),
    (0.028, 0.06, 0.9),
)


@dataclass(frozen=True, slots=True)
class ZonedLightState:
    entity_id: str
    zone: ScreenZone
    sample: ZoneSample
    update: LightUpdate


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    settings = load_settings()
    _configure_logging(settings.log_path)

    sync_lock = acquire_sync_singleton(LOGGER)
    if sync_lock is None:
        LOGGER.warning("Another MatterLights sync loop is already running; this instance will exit.")
        return 0

    client = HomeAssistantClient(
        settings.ha_url,
        settings.ha_token,
        timeout_seconds=settings.request_timeout_seconds,
        inter_light_delay_seconds=settings.inter_light_delay_seconds,
        max_parallel_updates=settings.max_parallel_light_updates,
    )
    light_zones = load_configured_light_zones(
        settings.light_zone_layout,
        settings.light_entities,
        settings.light_zone_file,
    )
    zone_file_mtime = _path_mtime(settings.light_zone_file)
    control_state = load_control_state(settings.control_state_file)
    control_file_mtime = _path_mtime(settings.control_state_file)
    custom_player = CustomPlayer()
    last_colors: dict[str, RgbColor] = {}
    off_entity_ids: set[str] = set()
    retry_entity_ids: set[str] = set()
    available_entity_ids = set(settings.light_entities)
    unavailable_entity_ids: set[str] = set()
    next_availability_refresh = 0.0
    display_off_active = False
    display_monitor = start_display_monitor(LOGGER) if settings.respect_display_sleep else None

    def reset_runtime_caches() -> None:
        last_colors.clear()
        off_entity_ids.clear()
        retry_entity_ids.clear()
        custom_player.reset()

    LOGGER.info(
        "Starting sync in %s mode for %s",
        control_state.mode,
        ", ".join(
            f"{entity_id}={zone.name}" for entity_id, zone in zip(settings.light_entities, light_zones)
        ),
    )

    try:
        with MSS() as screen_capture_session:
            while True:
                iteration_started = time.monotonic()
                try:
                    current_zone_file_mtime = _path_mtime(settings.light_zone_file)
                    if current_zone_file_mtime != zone_file_mtime:
                        light_zones = load_configured_light_zones(
                            settings.light_zone_layout,
                            settings.light_entities,
                            settings.light_zone_file,
                        )
                        zone_file_mtime = current_zone_file_mtime
                        LOGGER.info("Reloaded light zone layout")

                    current_control_file_mtime = _path_mtime(settings.control_state_file)
                    if current_control_file_mtime != control_file_mtime:
                        new_control_state = load_control_state(settings.control_state_file)
                        if new_control_state.mode != control_state.mode:
                            LOGGER.info("Playback mode set to %s", new_control_state.mode)
                        control_state = new_control_state
                        control_file_mtime = current_control_file_mtime
                        reset_runtime_caches()

                    current_time = time.monotonic()
                    if current_time >= next_availability_refresh:
                        available_entity_ids = client.get_available_entity_ids(settings.light_entities)
                        unavailable_entity_ids = set(settings.light_entities) - available_entity_ids
                        retry_entity_ids &= available_entity_ids
                        next_availability_refresh = current_time + settings.availability_refresh_seconds
                        if unavailable_entity_ids:
                            LOGGER.warning(
                                "Skipping unavailable lights: %s",
                                ", ".join(sorted(unavailable_entity_ids)),
                            )

                    display_on = display_monitor.is_display_on() if display_monitor is not None else True
                    if display_off_active and display_on:
                        display_off_active = False
                        reset_runtime_caches()
                        LOGGER.info("Display resumed; restoring lights")

                    ordered_available = [
                        entity_id for entity_id in settings.light_entities if entity_id in available_entity_ids
                    ]

                    if not display_on:
                        if not display_off_active:
                            if ordered_available:
                                client.turn_off_lights(ordered_available, settings.transition_seconds)
                            display_off_active = True
                            reset_runtime_caches()
                            LOGGER.info("Display off; turning lights off")
                    elif control_state.mode == MODE_CUSTOM:
                        command = custom_player.tick(control_state.custom, ordered_available, current_time)
                        if command is not None and command.updates:
                            # Many Matter bulbs lock up on a transition/fade command, so fades
                            # are capped (off by default) and only sent if explicitly enabled.
                            transition_seconds = min(
                                command.transition_seconds, settings.max_pattern_transition_seconds
                            )
                            failed_entity_ids = client.apply_light_updates(
                                command.updates,
                                transition_seconds,
                            )
                            if failed_entity_ids:
                                # Un-confirm only the failed lights so the next tick retries
                                # just them (snapped) instead of restarting the whole pattern.
                                custom_player.mark_failed(command.target_key, set(failed_entity_ids))
                                if len(failed_entity_ids) == len(command.updates):
                                    raise RuntimeError("No lights were updated successfully")
                                LOGGER.warning(
                                    "Custom playback failed for: %s",
                                    ", ".join(failed_entity_ids),
                                )
                    else:
                        captured_zone_samples = capture_zone_samples_with_session(
                            screen_capture_session,
                            settings.sample_stride,
                            settings.color_boost,
                            settings.screen_capture_target,
                            _capture_zones_for_mode(settings.color_sync_mode, light_zones),
                        )
                        zone_samples = _build_effective_zone_samples(
                            settings.color_sync_mode,
                            light_zones,
                            captured_zone_samples,
                            settings.primary_light_zone_names,
                        )
                        preview_overrides = load_preview_overrides(settings.preview_override_file)
                        desired_states = _build_desired_states(
                            settings.light_entities,
                            zone_samples,
                            available_entity_ids,
                            preview_overrides,
                            retry_entity_ids,
                            last_colors,
                            off_entity_ids,
                            settings.color_change_threshold,
                            settings.dark_threshold,
                            settings.dark_active_ratio_threshold,
                            settings.brightness_floor,
                        )

                        if desired_states:
                            failed_entity_ids = client.apply_light_updates(
                                [state.update for state in desired_states],
                                settings.transition_seconds,
                            )
                            if len(failed_entity_ids) == len(desired_states):
                                raise RuntimeError("No lights were updated successfully")

                            failed_entity_id_set = set(failed_entity_ids)
                            retry_entity_ids = failed_entity_id_set
                            _record_successful_states(desired_states, failed_entity_id_set, last_colors, off_entity_ids)

                            if failed_entity_ids:
                                LOGGER.warning(
                                    "Updated zoned lights with failures for: %s",
                                    ", ".join(failed_entity_ids),
                                )
                            else:
                                LOGGER.debug(
                                    "Updated %s zoned lights",
                                    len(desired_states),
                                )
                except KeyboardInterrupt:
                    raise
                except Exception:
                    LOGGER.exception(
                        "Sync iteration failed. Retrying in %.1f seconds.",
                        settings.error_retry_seconds,
                    )
                    time.sleep(settings.error_retry_seconds)
                    continue
                sleep_seconds = settings.sync_interval_seconds - (time.monotonic() - iteration_started)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Stopping sync")
        return 0
    except Exception:
        LOGGER.exception("Sync failed")
        return 1
    finally:
        if display_monitor is not None:
            display_monitor.stop()
        sync_lock.release()


def _configure_logging(log_path) -> None:
    root_logger = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if log_path is None:
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def _capture_zones_for_mode(color_sync_mode: str, light_zones: list[ScreenZone]) -> list[ScreenZone]:
    if color_sync_mode == "shared-variant":
        return list(AMBIENT_EDGE_CAPTURE_ZONES)
    return light_zones


def _build_effective_zone_samples(
    color_sync_mode: str,
    light_zones: list[ScreenZone],
    captured_zone_samples: list[ZoneSample],
    primary_light_zone_names: list[str],
) -> list[ZoneSample]:
    if color_sync_mode != "shared-variant":
        return captured_zone_samples
    if not captured_zone_samples:
        return []
    base_sample = _build_shared_variant_base_sample(captured_zone_samples)
    return _build_shared_variant_zone_samples(light_zones, base_sample, primary_light_zone_names)


def _build_shared_variant_base_sample(captured_zone_samples: list[ZoneSample]) -> ZoneSample:
    total_weight = 0.0
    brightness_total = 0.0
    active_ratio_total = 0.0
    dominant_color_buckets: dict[tuple[int, int, int], list[float]] = {}
    fallback_sample: ZoneSample | None = None
    fallback_score = -1.0

    for sample in captured_zone_samples:
        zone_weight = AMBIENT_EDGE_ZONE_WEIGHTS.get(sample.zone.name, 1.0)
        total_weight += zone_weight
        brightness_total += sample.average_brightness * zone_weight
        active_ratio_total += sample.active_ratio * zone_weight

        color = sample.color
        brightest_channel = color.max_channel()
        saturation = brightest_channel - min(color.red, color.green, color.blue)
        score = zone_weight * max(0.2, sample.active_ratio) * brightest_channel * max(1, saturation)
        if score > fallback_score:
            fallback_sample = sample
            fallback_score = score

        if brightest_channel == 0:
            continue

        saturation_ratio = saturation / brightest_channel
        if saturation < 20 or saturation_ratio < 0.18:
            continue

        bucket_key = _dominant_color_bucket(color)
        bucket_totals = dominant_color_buckets.get(bucket_key)
        weight = score * max(1, saturation)
        if bucket_totals is None:
            dominant_color_buckets[bucket_key] = [
                weight,
                color.red * weight,
                color.green * weight,
                color.blue * weight,
            ]
        else:
            bucket_totals[0] += weight
            bucket_totals[1] += color.red * weight
            bucket_totals[2] += color.green * weight
            bucket_totals[3] += color.blue * weight

    if dominant_color_buckets:
        dominant_bucket = max(dominant_color_buckets.values(), key=lambda totals: totals[0])
        dominant_weight = dominant_bucket[0]
        color = RgbColor(
            red=round(dominant_bucket[1] / dominant_weight),
            green=round(dominant_bucket[2] / dominant_weight),
            blue=round(dominant_bucket[3] / dominant_weight),
        )
    elif fallback_sample is not None:
        color = fallback_sample.color
    else:
        color = RgbColor(0, 0, 0)

    if total_weight == 0:
        return ZoneSample(FULL_SCREEN_ZONE, color, 0, 0.0)

    return ZoneSample(
        zone=FULL_SCREEN_ZONE,
        color=color,
        average_brightness=round(brightness_total / total_weight),
        active_ratio=active_ratio_total / total_weight,
    )


def _build_shared_variant_zone_samples(
    light_zones: list[ScreenZone],
    base_sample: ZoneSample,
    primary_light_zone_names: list[str],
) -> list[ZoneSample]:
    primary_zone_names = {zone_name.strip().lower() for zone_name in primary_light_zone_names if zone_name.strip()}
    primary_indices = [
        index for index, zone in enumerate(light_zones) if zone.name.strip().lower() in primary_zone_names
    ]
    if not primary_indices:
        primary_indices = list(range(min(2, len(light_zones))))

    primary_index_set = set(primary_indices)
    effective_samples: list[ZoneSample] = []
    secondary_index = 0
    for index, zone in enumerate(light_zones):
        color = base_sample.color
        if index not in primary_index_set:
            color = _apply_secondary_color_variant(base_sample.color, secondary_index)
            secondary_index += 1

        effective_samples.append(
            ZoneSample(
                zone=zone,
                color=color,
                average_brightness=base_sample.average_brightness,
                active_ratio=base_sample.active_ratio,
            )
        )
    return effective_samples


def _apply_secondary_color_variant(color: RgbColor, secondary_index: int) -> RgbColor:
    if color.max_channel() == 0:
        return color

    hue_shift, lightness_shift, saturation_scale = SECONDARY_COLOR_VARIANTS[
        secondary_index % len(SECONDARY_COLOR_VARIANTS)
    ]
    red = color.red / 255
    green = color.green / 255
    blue = color.blue / 255
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    hue = (hue + hue_shift) % 1.0
    lightness = min(1.0, max(0.0, lightness + lightness_shift))
    saturation = min(1.0, max(0.0, saturation * saturation_scale))
    shifted_red, shifted_green, shifted_blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return RgbColor(
        red=round(shifted_red * 255),
        green=round(shifted_green * 255),
        blue=round(shifted_blue * 255),
    )


def _dominant_color_bucket(color: RgbColor) -> tuple[int, int, int]:
    brightest_channel = color.max_channel()
    if brightest_channel == 0:
        return (0, 0, 0)

    bucket_size = 24
    normalized_red = (color.red * 255) // brightest_channel
    normalized_green = (color.green * 255) // brightest_channel
    normalized_blue = (color.blue * 255) // brightest_channel
    return (
        normalized_red // bucket_size,
        normalized_green // bucket_size,
        normalized_blue // bucket_size,
    )


def _should_send_update(last_color: RgbColor | None, new_color: RgbColor, threshold: int) -> bool:
    if last_color is None:
        return True
    return last_color.distance(new_color) >= threshold


def _build_desired_states(
    entity_ids: list[str],
    zone_samples: list[ZoneSample],
    available_entity_ids: set[str],
    preview_overrides,
    retry_entity_ids: set[str],
    last_colors: dict[str, RgbColor],
    off_entity_ids: set[str],
    color_change_threshold: int,
    dark_threshold: int,
    dark_active_ratio_threshold: float,
    brightness_floor: int,
) -> list[ZonedLightState]:
    desired_states: list[ZonedLightState] = []
    for entity_id, sample in zip(entity_ids, zone_samples):
        if entity_id not in available_entity_ids:
            continue

        preview_override = preview_overrides.get(entity_id)
        last_color = last_colors.get(entity_id)
        if preview_override is not None:
            preview_color = preview_override.color
            should_send_preview = (
                entity_id in retry_entity_ids
                or entity_id in off_entity_ids
                or last_color is None
                or last_color.distance(preview_color) > 0
            )
            if should_send_preview:
                desired_states.append(
                    ZonedLightState(
                        entity_id=entity_id,
                        zone=sample.zone,
                        sample=sample,
                        update=LightUpdate(
                            entity_id=entity_id,
                            color=preview_color,
                            brightness=preview_override.brightness,
                        ),
                    )
                )
            continue

        sampled_color = sample.color
        should_turn_off = sample.should_turn_off(dark_threshold, dark_active_ratio_threshold)
        should_send_update = (
            entity_id in retry_entity_ids
            or (should_turn_off and entity_id not in off_entity_ids)
            or (
                not should_turn_off
                and (entity_id in off_entity_ids or _should_send_update(last_color, sampled_color, color_change_threshold))
            )
        )
        if not should_send_update:
            continue

        if should_turn_off:
            update = LightUpdate(entity_id=entity_id)
        else:
            update = LightUpdate(
                entity_id=entity_id,
                color=sampled_color,
                brightness=sample.effective_brightness(brightness_floor),
            )
        desired_states.append(
            ZonedLightState(
                entity_id=entity_id,
                zone=sample.zone,
                sample=sample,
                update=update,
            )
        )
    return desired_states


def _record_successful_states(
    desired_states: list[ZonedLightState],
    failed_entity_ids: set[str],
    last_colors: dict[str, RgbColor],
    off_entity_ids: set[str],
) -> None:
    for state in desired_states:
        if state.entity_id in failed_entity_ids:
            continue
        last_colors[state.entity_id] = state.sample.color
        if state.update.color is None:
            off_entity_ids.add(state.entity_id)
        else:
            off_entity_ids.discard(state.entity_id)


def _path_mtime(path) -> float | None:
    if path is None or not path.exists():
        return None
    return path.stat().st_mtime


if __name__ == "__main__":
    raise SystemExit(main())
