"""Two machines, one set of bulbs: who may touch them, and what happens when the answer is unknown."""
from __future__ import annotations

import ast
import inspect
import logging
import unittest
from types import SimpleNamespace

from matterlights import main as main_module
from matterlights.owner import OwnerGate, owned_elsewhere, start_owner_gate

LOG = logging.getLogger("test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class Client:
    def __init__(self, *states):
        self.states = list(states)

    def get_state(self, entity_id):
        return self.states.pop(0)


def gate(*states):
    return OwnerGate("input_select.owner", "Rig", Client(*states), LOG)


class Gate(unittest.TestCase):
    def test_nothing_is_ours_before_the_first_reading(self):
        self.assertFalse(gate().owned())

    def test_it_follows_the_selector(self):
        g = gate("Rig", "Rig", "TV", "Rig", "Rig")
        answers = []
        for _ in range(5):
            g.tick()
            answers.append(g.owned())
        self.assertEqual(answers, [False, True, False, False, True])

    def test_taking_over_waits_for_a_second_reading_and_letting_go_does_not(self):
        g = gate("Rig", "Rig", "TV")
        g.tick()
        self.assertFalse(g.owned())
        g.tick()
        self.assertTrue(g.owned())
        g.tick()
        self.assertFalse(g.owned())

    def test_a_flicker_of_the_selector_does_not_take_the_lights(self):
        g = gate("Rig", "TV", "Rig")
        for _ in range(3):
            g.tick()
        self.assertFalse(g.owned())

    def test_no_reading_keeps_the_last_answer(self):
        for last in ("Rig", "TV"):
            g = gate(last, last, None)
            g.tick()
            g.tick()
            before = g.owned()
            g.tick()
            self.assertEqual(g.owned(), before)

    def test_no_reading_from_the_start_stays_hands_off(self):
        g = gate(None)
        g.tick()
        self.assertFalse(g.owned())


class Shutdown(unittest.TestCase):
    def settings(self, entity="input_select.owner"):
        return SimpleNamespace(ha_owner_entity=entity, ha_owner_value="Rig")

    def test_someone_elses_lights_are_left_alone(self):
        self.assertTrue(owned_elsewhere(self.settings(), Client("TV")))

    def test_our_own_are_not(self):
        self.assertFalse(owned_elsewhere(self.settings(), Client("Rig")))

    def test_no_reading_acts_as_before(self):
        self.assertFalse(owned_elsewhere(self.settings(), Client(None)))

    def test_no_owner_configured_never_asks(self):
        self.assertFalse(owned_elsewhere(self.settings(""), Client()))


class Starting(unittest.TestCase):
    def test_no_setting_no_gate(self):
        self.assertIsNone(start_owner_gate(SimpleNamespace(ha_owner_entity=""), Client(), LOG))

    def test_a_setting_starts_a_watch_that_stops(self):
        settings = SimpleNamespace(ha_owner_entity="input_select.owner", ha_owner_value="Rig", ha_switch_poll_seconds=0.01)
        g, thread = start_owner_gate(settings, Client(*["Rig"] * 1000), LOG)
        thread.stop()
        self.assertIsInstance(g, OwnerGate)


class TheLoopRespectsIt(unittest.TestCase):
    """main() is one long loop. Read as code: every lamp-touching path must sit behind the gate."""

    def setUp(self):
        self.source = inspect.getsource(main_module)

    def test_the_gate_comes_before_the_master_switch_and_the_display(self):
        gate_at = self.source.index("if not owned or not control_state.lights_on:")
        self.assertLess(gate_at, self.source.index("elif not display_on:"))
        self.assertLess(gate_at, self.source.index("elif control_state.mode == MODE_CUSTOM:"))

    def test_every_off_state_guards_against_a_late_colour(self):
        self.assertEqual(self.source.count("off_guard.entered()"), 2)
        self.assertEqual(self.source.count("off_guard.resweep_due()"), 2)

    def test_handing_over_abandons_what_is_on_its_way(self):
        at = self.source.index("standing_aside = True")
        self.assertIn("light_worker.abandon()", self.source[at:at + 120])

    def test_turning_lamps_off_in_that_branch_needs_ownership(self):
        branch = self.source[self.source.index("if not owned or not control_state.lights_on:"):self.source.index("elif not display_on:")]
        self.assertLess(branch.index("if not owned:"), branch.index("enforce_lights_off"))
        self.assertLess(branch.index("else:"), branch.index("enforce_lights_off"))

    def test_the_heartbeat_is_only_for_the_owner(self):
        at = self.source.index("client.post_heartbeat(")
        self.assertIn("and owned", self.source[at - 700:at])

    def test_the_shutdown_hook_asks(self):
        hook = self.source[self.source.index("def turn_off_all_lights"):self.source.index("client.turn_off_lights_urgently(")]
        self.assertIn("owner_gate.owned()", hook)

    def test_the_source_parses(self):
        ast.parse(self.source)


if __name__ == "__main__":
    unittest.main()
