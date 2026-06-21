from __future__ import annotations

import unittest

from matterlights.playback import (
    COLOR_WHITE,
    CUSTOM_PATTERN,
    CUSTOM_SOLID,
    MAX_KELVIN,
    MODE_AUTONOMOUS,
    MODE_CUSTOM,
    ControlState,
    CustomPlayer,
    CustomState,
    PatternStep,
    control_state_from_payload,
    control_state_to_payload,
    default_control_state,
    load_control_state,
    pattern_cycle_seconds,
    pattern_events,
)
from matterlights.screen import RgbColor


RED = RgbColor(255, 0, 0)
BLUE = RgbColor(0, 0, 255)
YELLOW = RgbColor(255, 255, 0)

# The pattern from the feature request: a 10-second loop.
EXAMPLE_STEPS = (
    PatternStep(RED, hold=3.0, transition=0.0),
    PatternStep(BLUE, hold=4.0, transition=1.0),
    PatternStep(YELLOW, hold=0.0, transition=2.0),
)


class PatternMathTests(unittest.TestCase):
    def test_cycle_length_sums_holds_and_transitions(self) -> None:
        self.assertEqual(pattern_cycle_seconds(EXAMPLE_STEPS), 10.0)

    def test_events_place_keyframes_at_transition_starts(self) -> None:
        events = pattern_events(EXAMPLE_STEPS)
        times = [round(start, 3) for start, _ in events]
        colors = [step.color.as_list() for _, step in events]
        transitions = [step.transition for _, step in events]
        self.assertEqual(times, [0.0, 3.0, 8.0])
        self.assertEqual(colors, [[255, 0, 0], [0, 0, 255], [255, 255, 0]])
        self.assertEqual(transitions, [0.0, 1.0, 2.0])

    def test_negative_values_are_treated_as_zero(self) -> None:
        steps = (PatternStep(RED, hold=-5.0, transition=-2.0), PatternStep(BLUE, hold=1.0, transition=1.0))
        self.assertEqual(pattern_cycle_seconds(steps), 2.0)


class CustomPlayerTests(unittest.TestCase):
    def _custom(self, **kwargs) -> CustomState:
        defaults = dict(type=CUSTOM_PATTERN, brightness=255, solid_color=RED, pattern_steps=EXAMPLE_STEPS)
        defaults.update(kwargs)
        return CustomState(**defaults)

    def test_pattern_only_fires_on_keyframe_boundaries(self) -> None:
        player = CustomPlayer()
        custom = self._custom()
        ids = ["light.a", "light.b"]

        fired: list[tuple[float, list[int], float]] = []
        now = 0.0
        while now < 10.0:
            command = player.tick(custom, ids, now)
            if command is not None:
                fired.append((round(now, 2), command.updates[0].color.as_list(), command.transition_seconds))
            now += 0.1

        # One emission per keyframe in a single 10s cycle, with HA-native transition.
        self.assertEqual([color for _, color, _ in fired], [[255, 0, 0], [0, 0, 255], [255, 255, 0]])
        self.assertEqual([transition for _, _, transition in fired], [0.0, 1.0, 2.0])
        self.assertEqual(fired[0][0], 0.0)
        self.assertAlmostEqual(fired[1][0], 3.0, delta=0.05)

    def test_pattern_targets_every_available_light_in_one_batch(self) -> None:
        player = CustomPlayer()
        command = player.tick(self._custom(), ["light.a", "light.b", "light.c"], 0.0)
        assert command is not None
        self.assertEqual([update.entity_id for update in command.updates], ["light.a", "light.b", "light.c"])
        self.assertTrue(all(update.color == RED for update in command.updates))
        self.assertTrue(all(update.brightness == 255 for update in command.updates))

    def test_solid_sends_once_then_stays_idle(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_SOLID, brightness=120, solid_color=BLUE)
        ids = ["light.a"]
        first = player.tick(custom, ids, 0.0)
        second = player.tick(custom, ids, 0.2)
        assert first is not None
        self.assertEqual(first.updates[0].color, BLUE)
        self.assertEqual(first.updates[0].brightness, 120)
        self.assertEqual(first.transition_seconds, 0.0)
        self.assertIsNone(second)

    def test_solid_resends_when_available_lights_change(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_SOLID, brightness=120, solid_color=BLUE)
        self.assertIsNotNone(player.tick(custom, ["light.a"], 0.0))
        self.assertIsNone(player.tick(custom, ["light.a"], 0.1))
        # A light coming back online forces a resend so it is not left stale.
        self.assertIsNotNone(player.tick(custom, ["light.a", "light.b"], 0.2))

    def test_reset_forces_a_fresh_emission(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_SOLID, brightness=120, solid_color=BLUE)
        self.assertIsNotNone(player.tick(custom, ["light.a"], 0.0))
        self.assertIsNone(player.tick(custom, ["light.a"], 0.1))
        player.reset()
        self.assertIsNotNone(player.tick(custom, ["light.a"], 0.2))

    def test_no_available_lights_emits_nothing(self) -> None:
        player = CustomPlayer()
        self.assertIsNone(player.tick(self._custom(), [], 0.0))

    def test_failed_light_is_retried_alone_without_resending_healthy_ones(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_SOLID, brightness=200, solid_color=BLUE)
        ids = ["light.a", "light.b", "light.c"]
        first = player.tick(custom, ids, 0.0)
        assert first is not None
        self.assertEqual([u.entity_id for u in first.updates], ids)

        # light.b failed to apply; only it should be retried next tick.
        player.mark_failed(first.target_key, {"light.b"})
        retry = player.tick(custom, ids, 0.1)
        assert retry is not None
        self.assertEqual([u.entity_id for u in retry.updates], ["light.b"])
        # Once everything is confirmed, nothing more is sent.
        self.assertIsNone(player.tick(custom, ids, 0.2))

    def test_single_bulb_failure_does_not_restart_the_pattern(self) -> None:
        player = CustomPlayer()
        custom = self._custom()  # the 10s red/blue/yellow loop
        ids = ["light.a", "light.b"]
        # Advance to the blue keyframe (t=3) and fail one bulb.
        player.tick(custom, ids, 0.0)
        blue = player.tick(custom, ids, 3.0)
        assert blue is not None
        self.assertEqual(blue.updates[0].color, BLUE)
        self.assertEqual(blue.transition_seconds, 1.0)
        player.mark_failed(blue.target_key, {"light.b"})

        # Still within the blue keyframe: retry light.b only, snapped (no re-fade),
        # and still BLUE — the pattern did not jump back to red.
        retry = player.tick(custom, ids, 3.4)
        assert retry is not None
        self.assertEqual([u.entity_id for u in retry.updates], ["light.b"])
        self.assertEqual(retry.updates[0].color, BLUE)
        self.assertEqual(retry.transition_seconds, 0.0)

    def test_single_step_pattern_behaves_like_solid(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_PATTERN, brightness=255, pattern_steps=(PatternStep(RED, 5.0, 1.0),))
        first = player.tick(custom, ["light.a"], 0.0)
        assert first is not None
        self.assertEqual(first.updates[0].color, RED)
        self.assertIsNone(player.tick(custom, ["light.a"], 2.0))
        self.assertIsNone(player.tick(custom, ["light.a"], 6.0))

    def test_empty_pattern_falls_back_to_solid_color(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_PATTERN, brightness=200, solid_color=YELLOW, pattern_steps=())
        command = player.tick(custom, ["light.a"], 0.0)
        assert command is not None
        self.assertEqual(command.updates[0].color, YELLOW)

    def test_solid_white_emits_color_temperature_not_rgb(self) -> None:
        player = CustomPlayer()
        custom = CustomState(type=CUSTOM_SOLID, brightness=180, solid_mode=COLOR_WHITE, solid_kelvin=3000)
        command = player.tick(custom, ["light.a", "light.b"], 0.0)
        assert command is not None
        update = command.updates[0]
        self.assertIsNone(update.color)
        self.assertEqual(update.color_temp_kelvin, 3000)
        self.assertEqual(update.brightness, 180)

    def test_switching_rgb_to_white_resends(self) -> None:
        player = CustomPlayer()
        rgb = CustomState(type=CUSTOM_SOLID, brightness=200, solid_color=BLUE)
        white = CustomState(type=CUSTOM_SOLID, brightness=200, solid_mode=COLOR_WHITE, solid_kelvin=4000)
        self.assertIsNotNone(player.tick(rgb, ["light.a"], 0.0))
        self.assertIsNone(player.tick(rgb, ["light.a"], 0.1))
        self.assertIsNotNone(player.tick(white, ["light.a"], 0.2))

    def test_pattern_white_step_emits_color_temperature(self) -> None:
        player = CustomPlayer()
        steps = (
            PatternStep(RED, hold=2.0, transition=0.0),
            PatternStep(RgbColor(0, 0, 0), hold=2.0, transition=0.0, mode=COLOR_WHITE, kelvin=5000),
        )
        custom = CustomState(type=CUSTOM_PATTERN, brightness=255, pattern_steps=steps)
        first = player.tick(custom, ["light.a"], 0.0)
        assert first is not None
        self.assertEqual(first.updates[0].color, RED)
        second = player.tick(custom, ["light.a"], 2.0)
        assert second is not None
        self.assertIsNone(second.updates[0].color)
        self.assertEqual(second.updates[0].color_temp_kelvin, 5000)


class SerializationTests(unittest.TestCase):
    def test_round_trip_preserves_state(self) -> None:
        state = ControlState(
            mode=MODE_CUSTOM,
            custom=CustomState(type=CUSTOM_PATTERN, brightness=180, solid_color=BLUE, pattern_steps=EXAMPLE_STEPS),
        )
        restored = control_state_from_payload(control_state_to_payload(state))
        self.assertEqual(restored.mode, MODE_CUSTOM)
        self.assertEqual(restored.custom.type, CUSTOM_PATTERN)
        self.assertEqual(restored.custom.brightness, 180)
        self.assertEqual(restored.custom.solid_color, BLUE)
        self.assertEqual(
            [(step.color.as_list(), step.hold, step.transition) for step in restored.custom.pattern_steps],
            [([255, 0, 0], 3.0, 0.0), ([0, 0, 255], 4.0, 1.0), ([255, 255, 0], 0.0, 2.0)],
        )

    def test_default_state_is_autonomous(self) -> None:
        self.assertEqual(default_control_state().mode, MODE_AUTONOMOUS)
        self.assertEqual(load_control_state(None).mode, MODE_AUTONOMOUS)

    def test_default_pattern_matches_documented_ten_second_loop(self) -> None:
        # The README and dashboard advertise a 10-second default loop.
        steps = default_control_state().custom.pattern_steps
        self.assertEqual(pattern_cycle_seconds(steps), 10.0)
        self.assertEqual([step.color.as_list() for step in steps], [[255, 0, 0], [0, 0, 255], [255, 255, 0]])

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode"):
            control_state_from_payload({"mode": "fast-and-furious"})

    def test_brightness_and_colors_are_clamped(self) -> None:
        state = control_state_from_payload(
            {
                "mode": "custom",
                "custom": {
                    "type": "solid",
                    "brightness": 9000,
                    "solid": {"color": [300, -5, 128]},
                },
            }
        )
        self.assertEqual(state.custom.brightness, 255)
        self.assertEqual(state.custom.solid_color, RgbColor(255, 0, 128))

    def test_malformed_color_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            control_state_from_payload(
                {"mode": "custom", "custom": {"type": "solid", "solid": {"color": [1, 2]}}}
            )

    def test_white_mode_round_trips(self) -> None:
        state = ControlState(
            mode=MODE_CUSTOM,
            custom=CustomState(
                type=CUSTOM_SOLID,
                brightness=200,
                solid_color=BLUE,
                solid_mode=COLOR_WHITE,
                solid_kelvin=3300,
                pattern_steps=(PatternStep(RED, 2.0, 1.0, mode=COLOR_WHITE, kelvin=5500),),
            ),
        )
        restored = control_state_from_payload(control_state_to_payload(state))
        self.assertEqual(restored.custom.solid_mode, COLOR_WHITE)
        self.assertEqual(restored.custom.solid_kelvin, 3300)
        self.assertEqual(restored.custom.pattern_steps[0].mode, COLOR_WHITE)
        self.assertEqual(restored.custom.pattern_steps[0].kelvin, 5500)

    def test_legacy_payload_without_mode_defaults_to_rgb(self) -> None:
        state = control_state_from_payload(
            {
                "mode": "custom",
                "custom": {
                    "type": "pattern",
                    "solid": {"color": [10, 20, 30]},
                    "pattern": {"steps": [{"color": [1, 2, 3], "hold": 1, "transition": 0}]},
                },
            }
        )
        self.assertEqual(state.custom.solid_mode, "rgb")
        self.assertEqual(state.custom.pattern_steps[0].mode, "rgb")

    def test_kelvin_is_clamped(self) -> None:
        state = control_state_from_payload(
            {"mode": "custom", "custom": {"type": "solid", "solid": {"mode": "white", "kelvin": 999999}}}
        )
        self.assertEqual(state.custom.solid_kelvin, MAX_KELVIN)

    def test_too_many_steps_are_rejected(self) -> None:
        steps = [{"color": [1, 2, 3], "hold": 1, "transition": 0} for _ in range(100)]
        with self.assertRaisesRegex(ValueError, "at most"):
            control_state_from_payload(
                {"mode": "custom", "custom": {"type": "pattern", "pattern": {"steps": steps}}}
            )


if __name__ == "__main__":
    unittest.main()
