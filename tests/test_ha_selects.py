"""The lights hub (2026-11-12): manual control, the mode and screen selects, the status sensor."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from matterlights.ha_selects import (
    MODE_SAVED_COLOUR, MODE_SAVED_PATTERN, MODE_SCREEN, SCREEN_ALL, SCREEN_PRIMARY, STATUS_REFRESH_SECONDS,
    SelectMirror, mode_label, screen_choices,
)
from matterlights.main import _hub_status
from matterlights.owner import OwnerGate, owned_elsewhere
from matterlights.playback import (
    CUSTOM_PATTERN, MODE_AUTONOMOUS, MODE_CUSTOM, ControlState, load_control_state, save_control_state,
    update_control_selection,
)

QUIET = logging.getLogger("test-quiet")
QUIET.addHandler(logging.NullHandler())
QUIET.propagate = False
MODE, SCREEN, STATUS, MANUAL, OWNER = "input_select.ml_mode", "input_select.ml_screen", "sensor.ml_status", "input_boolean.manual", "input_select.owner"
MONITORS = [
    {"index": 0, "left": 0, "top": 0, "width": 8640, "height": 3072, "is_primary": False, "name": ""},
    {"index": 1, "width": 1728, "height": 3072, "is_primary": False, "name": "HDMI-1"},
    {"index": 2, "width": 3840, "height": 2160, "is_primary": True, "name": "DP-2"},
]


class FakeHA:
    def __init__(self, **states):
        self.states = dict(states)
        self.options: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.posted: list[tuple] = []

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def select_option(self, entity_id, option):
        self.calls.append(("select", entity_id, option))
        self.states[entity_id] = option
        return True

    def set_select_options(self, entity_id, options):
        self.calls.append(("options", entity_id, tuple(options)))
        self.options[entity_id] = list(options)
        if self.states.get(entity_id) not in options:
            self.states[entity_id] = options[0]           # what Home Assistant does
        return True

    def post_state(self, entity_id, state, attributes):
        self.posted.append((entity_id, state, attributes))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "control.json"
        save_control_state(self.path, ControlState())

    def mirror(self, ha, clock=lambda: 0.0, **entities):
        return SelectMirror(self.path, ha, "primary", logger=QUIET, clock=clock, **entities)


class ManualControl(unittest.TestCase):
    def gate(self, ha, owner=True):
        return OwnerGate(OWNER if owner else "", "Rig" if owner else "", ha, QUIET, MANUAL)

    def test_manual_on_lets_go_at_once_whatever_the_selector_says(self):
        ha = FakeHA(**{OWNER: "Rig", MANUAL: "off"})
        gate = self.gate(ha)
        gate.tick(); gate.tick()
        self.assertTrue(gate.owned())
        ha.states[MANUAL] = "on"
        gate.tick()
        self.assertFalse(gate.owned())
        self.assertTrue(gate.manual())

    def test_manual_off_takes_over_on_the_second_reading(self):
        ha = FakeHA(**{OWNER: "Rig", MANUAL: "on"})
        gate = self.gate(ha)
        gate.tick()
        ha.states[MANUAL] = "off"
        gate.tick()
        self.assertFalse(gate.owned())
        gate.tick()
        self.assertTrue(gate.owned())

    def test_an_unreadable_manual_helper_keeps_its_last_answer(self):
        ha = FakeHA(**{OWNER: "Rig", MANUAL: "on"})
        gate = self.gate(ha)
        gate.tick()
        del ha.states[MANUAL]
        gate.tick(); gate.tick()
        self.assertFalse(gate.owned())

    def test_manual_alone_without_a_selector(self):
        ha = FakeHA(**{MANUAL: "off"})
        gate = self.gate(ha, owner=False)
        gate.tick(); gate.tick()
        self.assertTrue(gate.owned())
        ha.states[MANUAL] = "on"
        gate.tick()
        self.assertFalse(gate.owned())

    def test_the_shutdown_path_leaves_manual_lamps_alone(self):
        settings = SimpleNamespace(ha_owner_entity=OWNER, ha_owner_value="Rig", ha_manual_entity=MANUAL)
        self.assertTrue(owned_elsewhere(settings, FakeHA(**{OWNER: "Rig", MANUAL: "on"})))
        self.assertFalse(owned_elsewhere(settings, FakeHA(**{OWNER: "Rig", MANUAL: "off"})))
        self.assertFalse(owned_elsewhere(settings, FakeHA(**{OWNER: "Rig"})))    # no reading is not "someone else's"


class Mode(Base):
    def setUp(self):
        super().setUp()
        from matterlights.playback import CustomState, PatternStep
        from matterlights.screen import RgbColor
        save_control_state(self.path, ControlState(custom=CustomState(pattern_steps=(PatternStep(RgbColor(255, 0, 0), 2.0, 1.0),))))

    def test_first_reading_home_assistant_wins(self):
        ha = FakeHA(**{MODE: MODE_SAVED_PATTERN})
        self.mirror(ha, mode_entity=MODE).tick()
        state = load_control_state(self.path)
        self.assertEqual((state.mode, state.custom.type), (MODE_CUSTOM, CUSTOM_PATTERN))
        self.assertEqual(ha.calls, [])

    def test_a_change_here_is_copied_to_home_assistant(self):
        ha = FakeHA(**{MODE: MODE_SCREEN})
        m = self.mirror(ha, mode_entity=MODE)
        m.tick()
        update_control_selection(self.path, mode=MODE_CUSTOM, custom_type="solid")
        m.tick()
        self.assertEqual(ha.states[MODE], MODE_SAVED_COLOUR)

    def test_a_change_in_home_assistant_is_applied_here_and_keeps_the_rest(self):
        save_control_state(self.path, ControlState(lights_on=False, capture_target="2"))
        ha = FakeHA(**{MODE: MODE_SCREEN})
        m = self.mirror(ha, mode_entity=MODE)
        m.tick()
        ha.states[MODE] = MODE_SAVED_COLOUR
        m.tick()
        state = load_control_state(self.path)
        self.assertEqual(mode_label(state), MODE_SAVED_COLOUR)
        self.assertEqual((state.lights_on, state.capture_target), (False, "2"))

    def test_no_reading_and_unknown_options_change_nothing(self):
        ha = FakeHA()
        m = self.mirror(ha, mode_entity=MODE)
        m.tick()
        self.assertEqual(load_control_state(self.path).mode, MODE_AUTONOMOUS)
        ha.states[MODE] = "Disco"
        m.tick()
        self.assertEqual(load_control_state(self.path).mode, MODE_AUTONOMOUS)


    def test_a_pattern_with_no_saved_steps_is_refused_and_nothing_is_lost(self):
        save_control_state(self.path, ControlState(lights_on=False, capture_target="2"))   # no pattern saved
        ha = FakeHA(**{MODE: MODE_SCREEN})
        m = self.mirror(ha, mode_entity=MODE)
        m.tick()
        ha.states[MODE] = MODE_SAVED_PATTERN
        m.tick()
        state = load_control_state(self.path)
        self.assertEqual((state.mode, state.lights_on, state.capture_target), (MODE_AUTONOMOUS, False, "2"))
        self.assertEqual(ha.states[MODE], MODE_SCREEN)          # Home Assistant shows what really runs


class Screen(Base):
    def test_labels(self):
        labels = [label for label, _ in screen_choices(MONITORS)]
        self.assertEqual(labels, [SCREEN_PRIMARY, SCREEN_ALL, "Screen 1 · HDMI-1 · 1728×3072", "Screen 2 · DP-2 · 3840×2160 · primary"])
        windows = screen_choices([{"index": 1, "width": 3840, "height": 2160, "name": "\\\\.\\DISPLAY1"}])
        self.assertEqual(windows[-1][0], "Screen 1 · 3840×2160")

    def test_nothing_until_the_capture_loop_offers_the_monitors(self):
        ha = FakeHA(**{SCREEN: "Screen 9"})
        self.mirror(ha, screen_entity=SCREEN).tick()
        self.assertEqual(ha.calls, [])

    def test_this_machine_writes_the_options_and_wins_the_first_reading(self):
        save_control_state(self.path, ControlState(capture_target="2"))
        ha = FakeHA(**{SCREEN: SCREEN_ALL})
        m = self.mirror(ha, screen_entity=SCREEN)
        m.offer_monitors(MONITORS)
        m.tick()
        self.assertEqual(ha.options[SCREEN][2:], ["Screen 1 · HDMI-1 · 1728×3072", "Screen 2 · DP-2 · 3840×2160 · primary"])
        self.assertEqual(ha.states[SCREEN], "Screen 2 · DP-2 · 3840×2160 · primary")
        self.assertEqual(load_control_state(self.path).capture_target, "2")

    def test_a_choice_in_home_assistant_becomes_the_capture_screen(self):
        ha = FakeHA()
        m = self.mirror(ha, screen_entity=SCREEN)
        m.offer_monitors(MONITORS)
        m.tick()
        ha.states[SCREEN] = "Screen 1 · HDMI-1 · 1728×3072"
        m.tick()
        state = load_control_state(self.path)
        self.assertEqual(state.capture_target, "1")
        self.assertEqual(state.mode, MODE_AUTONOMOUS)

    def test_new_monitors_rewrite_the_options_and_repush_the_value(self):
        ha = FakeHA()
        m = self.mirror(ha, screen_entity=SCREEN)
        m.offer_monitors(MONITORS)
        m.tick()
        m.offer_monitors(MONITORS[:2])
        m.tick()
        self.assertEqual(ha.options[SCREEN], [SCREEN_PRIMARY, SCREEN_ALL, "Screen 1 · HDMI-1 · 1728×3072"])
        self.assertEqual(ha.states[SCREEN], SCREEN_PRIMARY)


class Status(Base):
    def test_posted_on_change_and_refreshed_but_not_repeated(self):
        now = [0.0]
        ha = FakeHA()
        m = self.mirror(ha, clock=lambda: now[0], status_entity=STATUS)
        m.tick()
        self.assertEqual(ha.posted, [])
        m.set_status("Following the screen", {"os": "Linux"})
        m.tick(); m.tick()
        self.assertEqual([p[1] for p in ha.posted], ["Following the screen"])
        m.set_status("Manual control", {"os": "Linux"})
        m.tick()
        now[0] = STATUS_REFRESH_SECONDS + 1
        m.tick()
        self.assertEqual([p[1] for p in ha.posted], ["Following the screen", "Manual control", "Manual control"])

    def test_the_words(self):
        settings = SimpleNamespace(screen_capture_target="primary", ha_owner_value="Rig")
        gate = SimpleNamespace(manual=lambda: False, holder=lambda: "TV")
        self.assertEqual(_hub_status(None, True, ControlState(), True, settings)[0], "Following the screen")
        self.assertEqual(_hub_status(None, True, ControlState(lights_on=False), True, settings)[0], "Lamps off")
        self.assertEqual(_hub_status(None, True, ControlState(), False, settings)[0], "Display asleep")
        self.assertEqual(_hub_status(gate, False, ControlState(), True, settings)[0], "TV has the lamps")
        manual = SimpleNamespace(manual=lambda: True, holder=lambda: "Rig")
        self.assertEqual(_hub_status(manual, False, ControlState(), True, settings)[0], "Manual control")
        starting = SimpleNamespace(manual=lambda: False, holder=lambda: "Rig")
        self.assertEqual(_hub_status(starting, False, ControlState(), True, settings)[0], "Starting")


class Selection(Base):
    def test_only_the_named_fields_change(self):
        save_control_state(self.path, ControlState(lights_on=False, rgb_on=False, capture_target="2"))
        update_control_selection(self.path, capture_target=None)
        state = load_control_state(self.path)
        self.assertEqual((state.capture_target, state.lights_on, state.rgb_on, state.mode), (None, False, False, MODE_AUTONOMOUS))
        with self.assertRaises(ValueError):
            update_control_selection(self.path, mode="disco")
