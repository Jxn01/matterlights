from __future__ import annotations

import colorsys
from dataclasses import dataclass
import logging
import time

from matterlights.ambience import (
    build_ambience,
    build_ambience_zone_samples,
    render_for_leds,
    resolve_near_entity_ids,
    split_near_far_samples,
)
from matterlights import capture as _capture
from matterlights.config import load_settings
from matterlights.display_power import start_display_monitor
from matterlights.home_assistant import HomeAssistantClient, LightUpdate
from matterlights.logging_setup import configure_logging
from matterlights.playback import MODE_CUSTOM, CustomPlayer, effective_capture_target, load_control_state
from matterlights.process_lock import acquire_sync_singleton
from matterlights.shutdown_hook import start_shutdown_hook
from matterlights.preview import load_preview_overrides
from matterlights.screen import (
    RgbColor,
    ScreenZone,
    ZoneSample,
    capture_raw_with_session,
    capture_session,
    load_configured_light_zones,
    sample_zone_samples_from_screenshot,
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


def enforce_lights_off(
    client: HomeAssistantClient,
    available_in_order: list[str],
    confirmed_off: set[str],
    transition_seconds: float,
) -> None:
    """Turn off every available light that is not already confirmed off.

    Call this on every tick an off-state holds, not just when it begins. The
    original code turned the lights off once on entry and then latched a flag,
    which silently did nothing whenever the bulbs happened to be unavailable at
    that instant: the log said "turning lights off", no request was sent, and
    the latch guaranteed it was never retried. Both logged display-sleep events
    hit exactly that case.

    Tracking which lights are *confirmed* off instead makes the state
    self-healing -- a bulb that was unavailable when the screen went to sleep,
    or that only came back later, is still turned off on a later sweep.

    Call this on entry to the off-state and then only when availability is
    refreshed, not on every tick: the sync loop ticks five times a second, and
    a bulb that is available but failing would otherwise be retried at that rate.
    """

    pending = [entity_id for entity_id in available_in_order if entity_id not in confirmed_off]
    if not pending:
        return

    # The client already logs each failure; adding a second line here would just
    # double the noise for a bulb that is refusing to answer.
    failed = set(client.turn_off_lights(pending, transition_seconds))
    confirmed_off.update(entity_id for entity_id in pending if entity_id not in failed)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    settings = load_settings()
    configure_logging(settings.log_path)

    # The capture backend needs the sync cadence (it caps the stream's frame
    # rate from it) and the configured default screen (to fall back to when the
    # selected one is unplugged). Neither is discoverable from inside capture.
    _capture.configure(settings.sync_interval_seconds, settings.screen_capture_target)

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
    next_heartbeat = 0.0
    display_off_active = False
    capture_fallback_active = False
    capture_inhibited = False
    screen_dark_active = False
    master_off_active = False
    display_monitor = start_display_monitor(LOGGER) if settings.respect_display_sleep else None

    # Optional local RGB extension. Imported lazily and only when configured, so
    # a disabled extension costs nothing -- not even an import -- and this
    # program behaves exactly as it did before the extension existed.
    rgb_publisher = None
    if settings.rgb_extension_enabled and settings.rgb_publish_socket is not None:
        from matterlights.rgb_publish import AmbiencePublisher

        rgb_publisher = AmbiencePublisher(settings.rgb_publish_socket, LOGGER)
        LOGGER.info("RGB extension enabled; publishing to %s", settings.rgb_publish_socket)

    def turn_off_all_lights() -> None:
        # Windows allows only a few seconds here. Every light gets identical
        # parameters, so this collapses into a single Home Assistant request --
        # but with a timeout of its own: the sync loop's is tuned for a loop that
        # retries every tick, and this call gets exactly one attempt before the
        # process is killed.
        client.turn_off_lights_urgently(
            list(settings.light_entities), settings.session_end_timeout_seconds
        )

    # Lights confirmed off by an off-state (master switch or display sleep).
    # Deliberately NOT part of reset_runtime_caches(), which clears
    # off_entity_ids on every state change and would forget what we sent.
    master_off_confirmed: set[str] = set()
    display_off_confirmed: set[str] = set()

    shutdown_hook = (
        start_shutdown_hook(turn_off_all_lights, LOGGER) if settings.turn_off_on_shutdown else None
    )

    def reset_runtime_caches() -> None:
        last_colors.clear()
        off_entity_ids.clear()
        retry_entity_ids.clear()
        custom_player.reset()

    ambience_near_ids = resolve_near_entity_ids(
        settings.ambience_near_lights,
        settings.primary_light_zone_names,
        settings.light_entities,
        light_zones,
    )

    LOGGER.info(
        "Starting sync in %s mode for %s",
        control_state.mode,
        ", ".join(
            f"{entity_id}={zone.name}" for entity_id, zone in zip(settings.light_entities, light_zones)
        ),
    )
    if settings.color_sync_mode == "ambience":
        LOGGER.info(
            "Ambience groups: near=%s far=%s",
            ", ".join(ambience_near_ids),
            ", ".join(e for e in settings.light_entities if e not in set(ambience_near_ids)),
        )

    try:
        with capture_session() as screen_capture_session:
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
                        ambience_near_ids = resolve_near_entity_ids(
                            settings.ambience_near_lights,
                            settings.primary_light_zone_names,
                            settings.light_entities,
                            light_zones,
                        )
                        LOGGER.info("Reloaded light zone layout")

                    current_control_file_mtime = _path_mtime(settings.control_state_file)
                    if current_control_file_mtime != control_file_mtime:
                        new_control_state = load_control_state(settings.control_state_file)
                        if new_control_state.mode != control_state.mode:
                            LOGGER.info("Playback mode set to %s", new_control_state.mode)
                        if new_control_state.capture_target != control_state.capture_target:
                            LOGGER.info(
                                "Capture screen set to %s",
                                new_control_state.capture_target or f"{settings.screen_capture_target} (default)",
                            )
                            capture_fallback_active = False
                        control_state = new_control_state
                        control_file_mtime = current_control_file_mtime
                        reset_runtime_caches()

                    current_time = time.monotonic()
                    if (
                        settings.heartbeat_entity_id
                        and settings.heartbeat_interval_seconds > 0
                        and current_time >= next_heartbeat
                    ):
                        # Deliberately before the availability refresh: if Home
                        # Assistant is unreachable this fails quietly, and the
                        # fallback automation treating that as "PC gone" is the
                        # correct reading anyway.
                        client.post_heartbeat(settings.heartbeat_entity_id)
                        next_heartbeat = current_time + settings.heartbeat_interval_seconds

                    availability_refreshed = False
                    if current_time >= next_availability_refresh:
                        available_entity_ids = client.get_available_entity_ids(settings.light_entities)
                        unavailable_entity_ids = set(settings.light_entities) - available_entity_ids
                        retry_entity_ids &= available_entity_ids
                        next_availability_refresh = current_time + settings.availability_refresh_seconds
                        availability_refreshed = True
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
                    if master_off_active and control_state.lights_on:
                        master_off_active = False
                        reset_runtime_caches()
                        LOGGER.info("Lights switched on from the dashboard; resuming")

                    ordered_available = [
                        entity_id for entity_id in settings.light_entities if entity_id in available_entity_ids
                    ]

                    if not control_state.lights_on:
                        # Master switch: off overrides every mode until re-enabled.
                        just_entered = not master_off_active
                        if just_entered:
                            master_off_active = True
                            master_off_confirmed.clear()
                            reset_runtime_caches()
                            LOGGER.info("Lights switched off from the dashboard")
                        # Sweep on entry, then only when availability changes --
                        # which is the only moment ordered_available can differ.
                        if just_entered or availability_refreshed:
                            enforce_lights_off(
                                client, ordered_available, master_off_confirmed, settings.transition_seconds
                            )
                        # The RGB extension has its own switch and is NOT silenced
                        # by the lamps' master switch -- switching the bulbs off at
                        # night should not also kill the case lighting. So keep
                        # capturing and publishing for it, without touching a lamp.
                        # Costs nothing unless the extension is enabled AND on.
                        if (
                            rgb_publisher is not None
                            and control_state.rgb_on
                            and display_on
                            and control_state.mode != MODE_CUSTOM
                        ):
                            _publish_for_rgb_only(
                                rgb_publisher,
                                screen_capture_session,
                                settings,
                                control_state,
                                ambience_near_ids,
                                last_colors,
                                display_on,
                            )
                    elif not display_on:
                        just_entered = not display_off_active
                        if just_entered:
                            display_off_active = True
                            display_off_confirmed.clear()
                            reset_runtime_caches()
                            LOGGER.info("Display off; turning lights off")
                        if just_entered or availability_refreshed:
                            enforce_lights_off(
                                client, ordered_available, display_off_confirmed, settings.transition_seconds
                            )
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
                        capture_target = effective_capture_target(control_state, settings.screen_capture_target)
                        try:
                            frame_raw, frame_width, frame_height = capture_raw_with_session(
                                screen_capture_session, capture_target
                            )
                            if capture_fallback_active:
                                capture_fallback_active = False
                                LOGGER.info("Capture screen %s is available again", capture_target)
                        except ValueError:
                            # The selected screen went away (unplugged/turned off). Keep the
                            # lights alive on the configured default instead of stalling.
                            if capture_target == settings.screen_capture_target:
                                raise
                            if not capture_fallback_active:
                                capture_fallback_active = True
                                LOGGER.warning(
                                    "Capture screen %s is unavailable; falling back to %s",
                                    capture_target,
                                    settings.screen_capture_target,
                                )
                            frame_raw, frame_width, frame_height = capture_raw_with_session(
                                screen_capture_session, settings.screen_capture_target
                            )

                        # Bound before the branch, not inside it: the publish
                        # below is outside, and only the ambience path produces a
                        # palette. Assigning it only in that branch leaves the
                        # name unbound for every other colour_sync_mode.
                        ambience_frame = None
                        if settings.color_sync_mode == "ambience":
                            zone_samples, ambience_frame = build_ambience(
                                frame_raw,
                                frame_width,
                                frame_height,
                                settings.sample_stride,
                                settings.color_boost,
                                settings.light_entities,
                                ambience_near_ids,
                                last_colors,
                            )
                        else:
                            captured_zone_samples = sample_zone_samples_from_screenshot(
                                frame_raw,
                                frame_width,
                                frame_height,
                                settings.sample_stride,
                                settings.color_boost,
                                _capture_zones_for_mode(settings.color_sync_mode, light_zones),
                            )
                            zone_samples = _build_effective_zone_samples(
                                settings.color_sync_mode,
                                light_zones,
                                captured_zone_samples,
                                settings.primary_light_zone_names,
                            )
                        # Edge-triggered, like the display-sleep events: dark detection
                        # otherwise turns the lights off in silence, which reads as a crash.
                        screen_dark = bool(zone_samples) and all(
                            sample.should_turn_off(
                                settings.dark_threshold, settings.dark_active_ratio_threshold
                            )
                            for sample in zone_samples
                        )
                        if screen_dark != screen_dark_active:
                            screen_dark_active = screen_dark
                            if screen_dark:
                                LOGGER.info("Watched screen (%s) went dark; turning lights off", capture_target)
                            else:
                                LOGGER.info("Watched screen (%s) is active again; restoring lights", capture_target)

                        if rgb_publisher is not None:
                            _publish_rgb_frame(
                                rgb_publisher,
                                zone_samples,
                                screen_dark=screen_dark,
                                display_on=display_on,
                                control_state=control_state,
                                settings=settings,
                                frame=ambience_frame,
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
                except _capture.CaptureUnavailable as error:
                    # Not a failure: the compositor refuses screencast sessions
                    # while the display is asleep or the session is locked. It is
                    # a normal part of the monitor powering down, and it resolves
                    # itself in well under a second once the display-power watcher
                    # catches up -- so retry on the ORDINARY interval. Using the
                    # error backoff here would turn a ~700 ms non-event into a
                    # five-second stall, every single time the screen sleeps.
                    if not capture_inhibited:
                        capture_inhibited = True
                        LOGGER.info("Capture inhibited (%s); waiting for the display", error)
                    time.sleep(settings.sync_interval_seconds)
                    continue
                except Exception:
                    LOGGER.exception(
                        "Sync iteration failed. Retrying in %.1f seconds.",
                        settings.error_retry_seconds,
                    )
                    time.sleep(settings.error_retry_seconds)
                    continue
                if capture_inhibited:
                    capture_inhibited = False
                    LOGGER.info("Capture is available again")
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
        if shutdown_hook is not None:
            shutdown_hook.stop()
        sync_lock.release()




def _publish_for_rgb_only(
    publisher, session, settings, control_state, ambience_near_ids, last_colors, display_on
) -> None:
    """Capture and publish for the RGB extension while the lamps are switched off.

    A cut-down version of the normal path: no availability sweep, no bulb
    updates, no capture-fallback bookkeeping. If the capture fails for any
    reason -- an inhibited compositor, an unplugged screen -- this tick is
    simply skipped. The lamps are already off; there is nothing to protect and
    nothing worth logging loudly about.
    """

    try:
        capture_target = effective_capture_target(control_state, settings.screen_capture_target)
        frame_raw, frame_width, frame_height = capture_raw_with_session(session, capture_target)
        zone_samples, ambience_frame = build_ambience(
            frame_raw,
            frame_width,
            frame_height,
            settings.sample_stride,
            settings.color_boost,
            settings.light_entities,
            ambience_near_ids,
            last_colors,
        )
    except Exception:  # noqa: BLE001 - the extension must never break the loop
        LOGGER.debug("RGB-only capture skipped", exc_info=True)
        return

    screen_dark = bool(zone_samples) and all(
        sample.should_turn_off(settings.dark_threshold, settings.dark_active_ratio_threshold)
        for sample in zone_samples
    )
    _publish_rgb_frame(
        publisher,
        zone_samples,
        screen_dark=screen_dark,
        display_on=display_on,
        control_state=control_state,
        settings=settings,
        frame=ambience_frame,
    )


def _publish_rgb_frame(
    publisher, zone_samples, *, screen_dark, display_on, control_state, settings, frame=None
) -> None:
    """Hand this tick's colours to the local RGB extension.

    Deliberately passes matterlights' own DECISIONS (``screen_dark``,
    ``lights_on``) rather than the raw numbers behind them. The extension
    mirrors the bulbs, and the only way to guarantee it agrees with them is for
    it to obey the same verdicts instead of recomputing darkness from a palette
    and drifting.

    ⚠️ ``near``/``far`` are the colours the bulbs **emit**, not the colours
    sampled off the screen -- :func:`render_for_leds` folds brightness into the
    triple, because the receiving hardware has no brightness channel of its own.
    Publishing the sampled colour instead is the bug this signature exists to
    prevent: it is not visibly wrong on a bright screen and renders the whole
    rig black on a dark one.

    Never raises: the publisher swallows its own failures, and this adds no new
    ones. The lights must not go dark because an RGB listener did.
    """

    near_sample, far_sample = split_near_far_samples(zone_samples)
    near, far = (
        render_for_leds(
            sample,
            brightness_floor=settings.brightness_floor,
            dark_threshold=settings.dark_threshold,
            dark_active_ratio_threshold=settings.dark_active_ratio_threshold,
        )
        for sample in (near_sample, far_sample)
    )
    # Boosted the same way the bulbs' colours are, so both consumers work in
    # one colour space rather than each inventing its own.
    palette = [
        ((entry.color.red, entry.color.green, entry.color.blue), entry.weight)
        for entry in (frame.palette if frame is not None else ())
    ]
    publisher.publish(
        (near.red, near.green, near.blue),
        (far.red, far.green, far.blue),
        palette=palette,
        # The near group's own numbers, so a consumer reading `brightness`
        # alongside `near` is reading one coherent sample rather than a mix.
        brightness=(
            near_sample.effective_brightness(settings.brightness_floor)
            if near_sample is not None
            else 0
        ),
        active_ratio=near_sample.active_ratio if near_sample is not None else 0.0,
        screen_dark=screen_dark,
        display_on=display_on,
        lights_on=control_state.lights_on,
        rgb_on=control_state.rgb_on,
    )


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
