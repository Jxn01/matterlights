from __future__ import annotations

import unittest

from matterlights.ambience import (
    _apportion_slots,
    _FAR_ZONE,
    _NEAR_ZONE,
    build_ambience_zone_samples,
    render_for_leds,
    resolve_near_entity_ids,
    sample_frame,
    split_near_far_samples,
)
from matterlights.screen import RgbColor, ScreenZone, ZoneSample


ENTITIES = ["light.a", "light.b", "light.c", "light.d", "light.e", "light.f"]
NEAR = ENTITIES[:2]

BROWN = (150, 110, 70)
RED = (255, 0, 0)
# A pair with matching perceived luminance, so neither dominates via brightness.
WARM = (200, 60, 60)
COOL = (60, 60, 200)


def make_raw(width: int, height: int, painter) -> bytes:
    raw = bytearray(width * height * 4)
    for y_pos in range(height):
        base = y_pos * width * 4
        for x_pos in range(width):
            red, green, blue = painter(x_pos, y_pos)
            index = base + x_pos * 4
            raw[index] = blue
            raw[index + 1] = green
            raw[index + 2] = red
            raw[index + 3] = 255
    return bytes(raw)


def build(raw, width=64, height=64, near=NEAR, last_colors=None, boost=1.0):
    return build_ambience_zone_samples(
        raw, width, height, 1, boost, ENTITIES, near, last_colors or {}
    )


def closest(color: RgbColor, anchors) -> tuple[int, int, int]:
    return min(anchors, key=lambda anchor: color.distance(RgbColor(*anchor)))


class RedSpotRegressionTests(unittest.TestCase):
    """The complaint that motivated the rewrite: a tiny vivid patch must not
    recolor the whole room."""

    def test_one_percent_red_spot_on_brown_screen_stays_brown(self) -> None:
        raw = make_raw(64, 64, lambda x, y: RED if (x < 6 and y < 6) else BROWN)
        samples = build(raw)
        for sample in samples:
            self.assertEqual(closest(sample.color, [BROWN, RED]), BROWN)

    def test_quarter_screen_red_becomes_a_single_far_accent(self) -> None:
        raw = make_raw(64, 64, lambda x, y: RED if (x < 32 and y < 32) else BROWN)
        samples = build(raw)
        red_entities = [
            entity
            for entity, sample in zip(ENTITIES, samples)
            if closest(sample.color, [BROWN, RED]) == RED
        ]
        # Red covers 25% of the area but supplies far less of the light, so it
        # earns exactly one bulb -- and in the far group, not beside the screen.
        self.assertEqual(len(red_entities), 1)
        self.assertNotIn(red_entities[0], NEAR)
        for entity, sample in zip(ENTITIES, samples):
            if entity in NEAR:
                self.assertEqual(closest(sample.color, [BROWN, RED]), BROWN)


class GroupRenderingTests(unittest.TestCase):
    def test_even_split_renders_both_components_in_both_groups(self) -> None:
        raw = make_raw(64, 64, lambda x, y: WARM if x < 32 else COOL)
        samples = build(raw)
        by_entity = dict(zip(ENTITIES, samples))

        near_families = {closest(by_entity[entity].color, [WARM, COOL]) for entity in NEAR}
        self.assertEqual(near_families, {WARM, COOL})

        far_families = [
            closest(by_entity[entity].color, [WARM, COOL])
            for entity in ENTITIES
            if entity not in NEAR
        ]
        self.assertEqual(far_families.count(WARM), 2)
        self.assertEqual(far_families.count(COOL), 2)

    def test_monochrome_screen_keeps_every_bulb_in_family(self) -> None:
        raw = make_raw(64, 64, lambda x, y: COOL)
        samples = build(raw)
        for sample in samples:
            self.assertEqual(closest(sample.color, [WARM, COOL]), COOL)

    def test_dark_screen_reports_dark_and_inactive(self) -> None:
        raw = make_raw(64, 64, lambda x, y: (4, 4, 4))
        samples = build(raw)
        for sample in samples:
            self.assertTrue(sample.should_turn_off(12, 0.05))

    def test_output_is_deterministic(self) -> None:
        raw = make_raw(64, 64, lambda x, y: WARM if (x + y) % 3 else COOL)
        first = build(raw)
        second = build(raw)
        self.assertEqual([s.color for s in first], [s.color for s in second])

    def test_samples_are_ordered_by_entity_and_tagged_by_group(self) -> None:
        raw = make_raw(64, 64, lambda x, y: BROWN)
        samples = build(raw)
        self.assertEqual(len(samples), len(ENTITIES))
        for entity, sample in zip(ENTITIES, samples):
            expected = "ambience-near" if entity in NEAR else "ambience-far"
            self.assertEqual(sample.zone.name, expected)


class ContinuityTests(unittest.TestCase):
    def test_dominance_flip_does_not_swap_bulb_colors(self) -> None:
        frame_a = make_raw(64, 64, lambda x, y: WARM if x < 36 else COOL)
        first = build(frame_a)
        last_colors = dict(zip(ENTITIES, (s.color for s in first)))
        warm_entities = {
            entity
            for entity, sample in zip(ENTITIES, first)
            if closest(sample.color, [WARM, COOL]) == WARM
        }

        # Dominance flips 56/44 -> 44/56; each bulb should keep its family.
        frame_b = make_raw(64, 64, lambda x, y: WARM if x < 28 else COOL)
        second = build(frame_b, last_colors=last_colors)
        for entity, sample in zip(ENTITIES, second):
            expected = WARM if entity in warm_entities else COOL
            self.assertEqual(closest(sample.color, [WARM, COOL]), expected)


class FrameStatsTests(unittest.TestCase):
    def test_average_brightness_and_active_ratio_match_definitions(self) -> None:
        raw = make_raw(64, 64, lambda x, y: (255, 255, 255) if y < 32 else (0, 0, 0))
        frame = sample_frame(raw, 64, 64, 1)
        self.assertAlmostEqual(frame.active_ratio, 0.5, delta=0.02)
        self.assertTrue(115 <= frame.average_brightness <= 135)

    def test_near_black_regions_supply_no_palette_color(self) -> None:
        # 60% near-black, 40% warm: the palette should be entirely warm because
        # darkness dims the output rather than tinting it gray.
        raw = make_raw(64, 64, lambda x, y: WARM if y >= 38 else (3, 3, 3))
        frame = sample_frame(raw, 64, 64, 1)
        self.assertTrue(frame.palette)
        for entry in frame.palette:
            self.assertEqual(closest(entry.color, [WARM, (3, 3, 3)]), WARM)


class ApportionmentTests(unittest.TestCase):
    def test_exact_shares(self) -> None:
        self.assertEqual(_apportion_slots([1.0], 4), [4])
        self.assertEqual(_apportion_slots([0.5, 0.5], 4), [2, 2])
        self.assertEqual(_apportion_slots([0.83, 0.17], 4), [3, 1])

    def test_largest_remainder_breaks_ties_toward_dominant(self) -> None:
        self.assertEqual(_apportion_slots([0.6, 0.25, 0.1, 0.05], 4), [3, 1, 0, 0])

    def test_two_slots_carry_the_top_two_components(self) -> None:
        # The runner-up's remainder (0.5) beats the dominant's (0.2), so the
        # near pair renders the top two components rather than doubling one.
        self.assertEqual(_apportion_slots([0.6, 0.25, 0.1, 0.05], 2), [1, 1, 0, 0])
        self.assertEqual(_apportion_slots([0.56, 0.44], 2), [1, 1])
        self.assertEqual(_apportion_slots([0.9, 0.1], 2), [2, 0])


class NearGroupResolutionTests(unittest.TestCase):
    ZONES = [ScreenZone(name, 0.0, 0.0, 1.0, 1.0) for name in
             ["top-left", "top-center", "top-right", "bottom-right", "bottom-center", "bottom-left"]]

    def test_explicit_setting_wins(self) -> None:
        result = resolve_near_entity_ids(["light.d", "light.b"], ["top-center"], ENTITIES, self.ZONES)
        self.assertEqual(result, ["light.b", "light.d"])  # entity order preserved

    def test_falls_back_to_primary_zone_names(self) -> None:
        result = resolve_near_entity_ids([], ["top-center", "bottom-left"], ENTITIES, self.ZONES)
        self.assertEqual(result, ["light.b", "light.f"])

    def test_falls_back_to_first_two_entities(self) -> None:
        result = resolve_near_entity_ids([], ["nonexistent"], ENTITIES, self.ZONES)
        self.assertEqual(result, ["light.a", "light.b"])


if __name__ == "__main__":
    unittest.main()


class RenderForLedsTest(unittest.TestCase):
    """🚨 The regression guard for a rig that looked switched off for a day.

    A bulb takes ``rgb_color`` and ``brightness`` as two separate arguments and
    Home Assistant normalises the colour, so ``(24, 24, 23)`` at brightness 255
    is emitted as a bright warm white. An addressable LED has no brightness
    channel -- its brightness *is* the magnitude of its triple -- so publishing
    the sampled colour handed the RGB extension a 9%-grey and the whole case
    read as dead while the lamps beside it looked correct.
    """

    def _sample(self, colour, brightness=200, active_ratio=0.5):
        return ZoneSample(
            zone=ScreenZone("z", 0.0, 0.0, 1.0, 1.0),
            color=RgbColor(*colour),
            average_brightness=brightness,
            active_ratio=active_ratio,
        )

    def _render(self, sample, floor=255):
        return render_for_leds(
            sample,
            brightness_floor=floor,
            dark_threshold=12,
            dark_active_ratio_threshold=0.05,
        )

    def test_a_dim_neutral_sample_renders_at_full_brightness(self) -> None:
        """The exact case that was reported: a dark terminal on this rig."""

        self.assertEqual(self._render(self._sample((24, 24, 23))), RgbColor(255, 255, 244))

    def test_hue_is_preserved_while_magnitude_is_raised(self) -> None:
        rendered = self._render(self._sample((12, 6, 0)))
        self.assertEqual(rendered, RgbColor(255, 128, 0))

    def test_an_already_full_colour_is_unchanged(self) -> None:
        self.assertEqual(self._render(self._sample((255, 0, 0))), RgbColor(255, 0, 0))

    def test_a_lower_floor_scales_the_magnitude_down(self) -> None:
        """With no floor the sample's own brightness sets the magnitude."""

        rendered = self._render(self._sample((24, 24, 24), brightness=128), floor=0)
        self.assertEqual(rendered, RgbColor(128, 128, 128))

    def test_a_sample_the_bulbs_would_turn_off_renders_black(self) -> None:
        dark = self._sample((2, 2, 2), brightness=3, active_ratio=0.0)
        self.assertEqual(self._render(dark), RgbColor(0, 0, 0))

    def test_pure_black_renders_black_rather_than_dividing_by_zero(self) -> None:
        self.assertEqual(self._render(self._sample((0, 0, 0), brightness=200)), RgbColor(0, 0, 0))

    def test_no_sample_renders_black(self) -> None:
        self.assertEqual(self._render(None), RgbColor(0, 0, 0))

    def test_it_never_leaves_the_channel_range(self) -> None:
        for colour in ((1, 0, 0), (255, 254, 253), (3, 200, 7), (1, 1, 1)):
            with self.subTest(colour=colour):
                rendered = self._render(self._sample(colour))
                for channel in (rendered.red, rendered.green, rendered.blue):
                    self.assertGreaterEqual(channel, 0)
                    self.assertLessEqual(channel, 255)


class SplitNearFarSamplesTest(unittest.TestCase):
    def _sample(self, zone, colour):
        return ZoneSample(zone=zone, color=RgbColor(*colour), average_brightness=100, active_ratio=0.5)

    def test_it_returns_one_sample_per_group(self) -> None:
        near = self._sample(_NEAR_ZONE, (10, 0, 0))
        far = self._sample(_FAR_ZONE, (0, 10, 0))
        self.assertEqual(split_near_far_samples([near, far]), (near, far))

    def test_one_empty_group_falls_back_to_the_other(self) -> None:
        near = self._sample(_NEAR_ZONE, (10, 0, 0))
        self.assertEqual(split_near_far_samples([near]), (near, near))

    def test_no_samples_gives_no_samples(self) -> None:
        self.assertEqual(split_near_far_samples([]), (None, None))
