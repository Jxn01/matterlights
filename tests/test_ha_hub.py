"""The lights hub, part two (2026-11-12): every setting the localhost pages held, mirrored to Home Assistant helpers."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from matterlights.ha_hub import TEXT_MAX, ZONE_CUSTOM, ZONE_OPTIONS, HubMirror, hub_entities, start_hub_mirror
from matterlights.playback import (
    COLOR_RGB,
    COLOR_WHITE,
    MAX_PATTERN_STEPS,
    ControlState,
    PatternStep,
    format_pattern,
    load_control_state,
    parse_pattern,
    save_control_state,
    update_control_selection,
)
from matterlights.screen import RgbColor, ScreenZone, load_configured_light_zones, save_light_zones

QUIET = logging.getLogger("test-hub-quiet")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False
P = "ml"
E = hub_entities(P, 2)
LIGHTS = ["light.a", "light.b"]


class FakeHA:
    """Home Assistant's helpers: a state per entity, and the services that set them."""

    def __init__(self, **states):
        self.states = dict(states)
        self.calls: list[tuple] = []

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        entity_id = data["entity_id"]
        value = data.get("option", data.get("value"))
        self.states[entity_id] = str(value) if not isinstance(value, float) else f"{value:.1f}"
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        base = Path(self.dir.name)
        self.path = base / "control.json"
        self.zones = base / "zones.json"
        save_control_state(self.path, ControlState())
        self.settings = SimpleNamespace(
            control_state_file=self.path, ha_hub_prefix=P, light_entities=list(LIGHTS),
            light_zone_layout=["top-left", "top-right"], light_zone_file=self.zones, color_sync_mode="ambience",
            ha_switch_poll_seconds=1.0,
        )
        self.restarts = 0

    def restart(self):
        self.restarts += 1

    def agreeing_ha(self, **overrides) -> FakeHA:
        """A Home Assistant that already shows what the control file holds (what lights_hub.py seeds)."""

        state = load_control_state(self.path)
        values = {
            E["brightness"]: f"{round(state.custom.brightness * 100 / 255)}.0",
            E["colour_type"]: "Colour" if state.custom.solid_mode == COLOR_RGB else "White",
            E["colour"]: "#{:02x}{:02x}{:02x}".format(*state.custom.solid_color.as_list()),
            E["kelvin"]: f"{state.custom.solid_kelvin}.0",
            E["pattern"]: format_pattern(state.custom.pattern_steps),
            E["engine"]: "Ambience",
            E["zone_1"]: "top-left",
            E["zone_2"]: "top-right",
            E["restart"]: "2026-11-12T10:00:00+00:00",
        }
        values.update(overrides)
        return FakeHA(**values)

    def mirror(self, ha) -> HubMirror:
        return HubMirror(self.settings, ha, self.restart, QUIET)

    def custom(self):
        return load_control_state(self.path).custom


class PatternText(unittest.TestCase):
    def test_round_trip(self):
        steps = (PatternStep(RgbColor(255, 51, 0), 2.0, 1.0), PatternStep(RgbColor(255, 255, 255), 3.0, 0.5, COLOR_WHITE, 2700))
        self.assertEqual(format_pattern(steps), "#ff3300 2 1, 2700K 3 0.5")
        self.assertEqual(parse_pattern("#ff3300 2 1, 2700K 3 0.5"), steps)

    def test_semicolons_spaces_and_case(self):
        self.assertEqual(parse_pattern(" #FF3300 2 ;  2700k 3 "), parse_pattern("#ff3300 2 0, 2700K 3"))

    def test_mistakes_name_the_step(self):
        for text, fragment in (("#ff3300 2, purple 3", "step 2"), ("#ff3300", "step 1"), ("#ff3300 x", "step 1"),
                               ("#ff33 2", "neither")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    parse_pattern(text)
                self.assertIn(fragment, str(caught.exception))

    def test_a_white_out_of_range_is_clamped_like_the_control_file_does(self):
        self.assertEqual(parse_pattern("1000K 2")[0].kelvin, 1500)
        self.assertEqual(parse_pattern("9000K 2")[0].kelvin, 8000)

    def test_an_empty_pattern_is_refused(self):
        for text in ("", "  ", ",;,"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "at least one step"):
                parse_pattern(text)

    def test_at_most_the_step_limit(self):
        parse_pattern(", ".join(["#ffffff 1"] * MAX_PATTERN_STEPS))
        with self.assertRaisesRegex(ValueError, "at most"):
            parse_pattern(", ".join(["#ffffff 1"] * (MAX_PATTERN_STEPS + 1)))


class Entities(unittest.TestCase):
    def test_names(self):
        self.assertEqual(hub_entities("matterlights", 2), {
            "brightness": "input_number.matterlights_brightness",
            "colour_type": "input_select.matterlights_colour_type",
            "colour": "input_text.matterlights_colour",
            "kelvin": "input_number.matterlights_kelvin",
            "pattern": "input_text.matterlights_pattern",
            "engine": "input_select.matterlights_engine",
            "restart": "input_button.matterlights_restart",
            "zone_1": "input_select.matterlights_zone_1",
            "zone_2": "input_select.matterlights_zone_2",
        })

    def test_zone_options_end_with_custom(self):
        self.assertEqual(ZONE_OPTIONS[-1], ZONE_CUSTOM)
        self.assertIn("full", ZONE_OPTIONS)


class FirstReading(Base):
    def test_agreement_changes_nothing(self):
        ha = self.agreeing_ha()
        before = self.path.read_bytes()
        self.mirror(ha).tick()
        self.assertEqual(ha.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.zones.exists())   # the configured layout already matched; nothing was written

    def test_home_assistant_wins(self):
        ha = self.agreeing_ha(**{E["brightness"]: "50.0", E["colour"]: "#00ff00", E["colour_type"]: "White",
                                 E["kelvin"]: "4000.0", E["pattern"]: "#0000ff 2 1, 3000K 4", E["engine"]: "Zoned"})
        self.mirror(ha).tick()
        custom = self.custom()
        self.assertEqual(custom.brightness, 128)
        self.assertEqual(custom.solid_color, RgbColor(0, 255, 0))
        self.assertEqual(custom.solid_mode, COLOR_WHITE)
        self.assertEqual(custom.solid_kelvin, 4000)
        self.assertEqual(format_pattern(custom.pattern_steps), "#0000ff 2 1, 3000K 4")
        self.assertEqual(load_control_state(self.path).color_sync_mode, "zoned")
        self.assertEqual(ha.calls, [])

    def test_no_reading_is_not_a_value(self):
        ha = FakeHA()
        before = self.path.read_bytes()
        m = self.mirror(ha)
        m.tick(); m.tick()
        self.assertEqual(ha.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.restarts, 0)


class BothWays(Base):
    def test_a_local_change_is_pushed(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        update_control_selection(self.path, brightness=128, solid_color=RgbColor(1, 2, 3))
        m.tick()
        self.assertIn(("input_number", "set_value", {"entity_id": E["brightness"], "value": 50.0}), ha.calls)
        self.assertIn(("input_text", "set_value", {"entity_id": E["colour"], "value": "#010203"}), ha.calls)
        ha.calls.clear()
        m.tick()
        self.assertEqual(ha.calls, [])           # agreed now: no second push

    def test_a_home_assistant_change_is_applied(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        ha.states[E["brightness"]] = "20.0"
        ha.states[E["colour_type"]] = "White"
        m.tick()
        self.assertEqual(self.custom().brightness, 51)
        self.assertEqual(self.custom().solid_mode, COLOR_WHITE)
        self.assertEqual(ha.calls, [])           # nothing echoed back

    def test_rounding_is_not_a_change(self):
        """1 % is 2.55 of 255: stored as 3, shown as 1 again -- not a ping-pong."""

        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        ha.states[E["brightness"]] = "1.0"
        m.tick(); m.tick(); m.tick()
        self.assertEqual(self.custom().brightness, 3)
        self.assertEqual(ha.calls, [])

    def test_a_bad_value_is_kept_in_home_assistant_reported_and_applied_once_corrected(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        before = self.custom().pattern_steps
        ha.states[E["pattern"]] = "#ff0000 2, purple 3"
        m.tick(); m.tick()
        self.assertEqual(self.custom().pattern_steps, before)
        self.assertEqual(ha.states[E["pattern"]], "#ff0000 2, purple 3")   # not overwritten
        self.assertIn("step 2", m.problems()[E["pattern"]])
        ha.states[E["pattern"]] = "#ff0000 2, #800080 3"
        m.tick()
        self.assertEqual(format_pattern(self.custom().pattern_steps), "#ff0000 2, #800080 3")
        self.assertNotIn(E["pattern"], m.problems())

    def test_a_bad_value_at_the_first_reading_leaves_both_sides(self):
        ha = self.agreeing_ha(**{E["colour"]: "green"})
        before = self.custom().solid_color
        m = self.mirror(ha)
        m.tick(); m.tick()
        self.assertEqual(self.custom().solid_color, before)
        self.assertEqual(ha.states[E["colour"]], "green")
        self.assertIn("#rrggbb", m.problems()[E["colour"]])

    def test_a_pattern_too_long_for_home_assistant_is_reported_not_sent(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        long = tuple(PatternStep(RgbColor(i, i, i), 12.5, 1.25) for i in range(MAX_PATTERN_STEPS))
        update_control_selection(self.path, pattern_steps=long)
        self.assertGreater(len(format_pattern(long)), TEXT_MAX)
        m.tick()
        self.assertNotIn(E["pattern"], [c[2]["entity_id"] for c in ha.calls])
        self.assertIn("characters", m.problems()[E["pattern"]])


class Engine(Base):
    def test_the_environment_default_is_stored_as_inherit(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        ha.states[E["engine"]] = "Shared variant"
        m.tick()
        self.assertEqual(load_control_state(self.path).color_sync_mode, "shared-variant")
        ha.states[E["engine"]] = "Ambience"
        m.tick()
        self.assertIsNone(load_control_state(self.path).color_sync_mode)

    def test_an_unknown_label_is_a_problem(self):
        ha = self.agreeing_ha(**{E["engine"]: "Rainbow"})
        m = self.mirror(ha)
        m.tick()
        self.assertIsNone(load_control_state(self.path).color_sync_mode)
        self.assertIn(E["engine"], m.problems())


class Zones(Base):
    def zones_now(self):
        return load_configured_light_zones(self.settings.light_zone_layout, LIGHTS, self.zones)

    def test_a_preset_from_home_assistant_is_saved_to_the_zones_file(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        ha.states[E["zone_2"]] = "full"
        m.tick()
        names = [z.name for z in self.zones_now()]
        self.assertEqual(names, ["top-left", "full"])
        self.assertEqual(ha.calls, [])

    def test_a_hand_drawn_zone_shows_as_custom(self):
        save_light_zones(self.zones, LIGHTS, [ScreenZone("mine", 0.1, 0.1, 0.4, 0.4), ScreenZone("top-right", 0.62, 0.0, 1.0, 0.35)])
        ha = self.agreeing_ha(**{E["zone_1"]: ZONE_CUSTOM})
        m = self.mirror(ha)
        m.tick()
        self.assertEqual(ha.calls, [])
        self.assertEqual(self.zones_now()[0].name, "mine")

    def test_a_refused_pick_is_forgotten_once_the_sides_agree_again(self):
        """Live, 2026-11-12: 'custom' refused, then the hand-drawn file came back -- both sides said custom, and the
        old 'cannot apply' stayed in setting_problems for good."""

        save_light_zones(self.zones, LIGHTS, [ScreenZone("mine", 0.1, 0.1, 0.4, 0.4), ScreenZone("top-right", 0.62, 0.0, 1.0, 0.35)])
        hand_drawn = self.zones.read_bytes()
        ha = self.agreeing_ha(**{E["zone_1"]: ZONE_CUSTOM})
        m = self.mirror(ha)
        m.tick()
        ha.states[E["zone_1"]] = "full"
        m.tick()
        ha.states[E["zone_1"]] = ZONE_CUSTOM
        m.tick()
        self.assertIn(E["zone_1"], m.problems())
        self.zones.write_bytes(hand_drawn)
        m.tick()
        self.assertNotIn(E["zone_1"], m.problems())

    def test_an_unreadable_zones_file_is_a_problem_until_it_reads_again(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        good = load_configured_light_zones(self.settings.light_zone_layout, LIGHTS, self.zones)
        self.zones.write_text("{ not json", encoding="utf-8")
        m.tick()
        self.assertIn(E["zone_1"], m.problems())
        save_light_zones(self.zones, LIGHTS, good)
        m.tick()
        self.assertNotIn(E["zone_1"], m.problems())

    def test_custom_cannot_be_picked_over_a_preset(self):
        ha = self.agreeing_ha()
        m = self.mirror(ha)
        m.tick()
        ha.states[E["zone_1"]] = ZONE_CUSTOM
        m.tick()
        self.assertEqual(self.zones_now()[0].name, "top-left")
        self.assertIn("preset", m.problems()[E["zone_1"]])


class Restart(Base):
    STARTED = "2026-11-12T10:00:00+00:00"

    def mirror(self, ha) -> HubMirror:
        from datetime import datetime

        return HubMirror(self.settings, ha, self.restart, QUIET, started=datetime.fromisoformat(self.STARTED))

    def test_an_old_press_never_restarts_a_new_one_does_once(self):
        ha = self.agreeing_ha(**{E["restart"]: "2026-11-12T09:59:00+00:00"})
        m = self.mirror(ha)
        m.tick(); m.tick()
        self.assertEqual(self.restarts, 0)
        ha.states[E["restart"]] = "2026-11-12T10:05:00.123456+00:00"
        m.tick(); m.tick()
        self.assertEqual(self.restarts, 1)

    def test_the_very_first_press_of_a_new_button_counts(self):
        """A new input_button is 'unknown' (no reading) until pressed: that first press must not be swallowed."""

        ha = self.agreeing_ha()
        del ha.states[E["restart"]]
        m = self.mirror(ha)
        m.tick(); m.tick()
        ha.states[E["restart"]] = "2026-11-12T10:01:00+00:00"
        m.tick()
        self.assertEqual(self.restarts, 1)

    def test_home_assistant_coming_back_is_not_a_press(self):
        ha = self.agreeing_ha(**{E["restart"]: "2026-11-12T09:00:00+00:00"})
        m = self.mirror(ha)
        m.tick()
        del ha.states[E["restart"]]
        m.tick()
        ha.states[E["restart"]] = "2026-11-12T09:00:00+00:00"
        m.tick()
        self.assertEqual(self.restarts, 0)

    def test_the_restarted_copy_does_not_restart_again(self):
        ha = self.agreeing_ha(**{E["restart"]: "2026-11-12T09:59:59+00:00"})   # the press that restarted us
        m = self.mirror(ha)
        m.tick(); m.tick()
        self.assertEqual(self.restarts, 0)


class RestartInPlace(unittest.TestCase):
    """main() returning RESTART_EXIT_CODE makes run() start the same command again -- per OS, the right way."""

    def run_with(self, platform: str, code: int):
        from unittest import mock

        from matterlights import main as main_module

        with mock.patch.object(main_module, "main", return_value=code), \
                mock.patch.object(main_module.sys, "platform", platform), \
                mock.patch.object(main_module.sys, "argv", ["/x/__main__.py", "--flag"]), \
                mock.patch("os.execv") as execv, mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(SystemExit) as exited:
                main_module.run()
        return exited.exception.code, execv, popen

    def test_linux_replaces_the_process(self):
        import sys

        code, execv, popen = self.run_with("linux", 75)
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "matterlights", "--flag"])
        popen.assert_not_called()

    def test_windows_starts_a_fresh_copy_and_exits_cleanly(self):
        import sys

        code, execv, popen = self.run_with("win32", 75)
        execv.assert_not_called()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "matterlights", "--flag"])
        self.assertEqual(code, 0)

    def test_any_other_code_just_exits(self):
        code, execv, popen = self.run_with("linux", 1)
        self.assertEqual(code, 1)
        execv.assert_not_called()
        popen.assert_not_called()


class Start(Base):
    def test_off_without_a_prefix_or_a_control_file(self):
        self.settings.ha_hub_prefix = ""
        self.assertIsNone(start_hub_mirror(self.settings, FakeHA(), self.restart, QUIET))
        self.settings.ha_hub_prefix = P
        self.settings.control_state_file = None
        self.assertIsNone(start_hub_mirror(self.settings, FakeHA(), self.restart, QUIET))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
