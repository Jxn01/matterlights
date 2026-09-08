from __future__ import annotations

import unittest

from matterlights.main import enforce_lights_off


class FakeClient:
    """Records what was asked of Home Assistant and can fail chosen lights."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing or set()

    def turn_off_lights(self, entity_ids: list[str], transition_seconds: float) -> list[str]:
        self.calls.append(list(entity_ids))
        return [entity_id for entity_id in entity_ids if entity_id in self.failing]


class EnforceLightsOffTests(unittest.TestCase):
    def test_turns_off_available_lights_and_remembers_them(self) -> None:
        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)

        self.assertEqual(client.calls, [["a", "b"]])
        self.assertEqual(confirmed, {"a", "b"})

    def test_does_not_resend_to_lights_already_confirmed_off(self) -> None:
        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a"], confirmed, 0.0)
        enforce_lights_off(client, ["a"], confirmed, 0.0)

        self.assertEqual(client.calls, [["a"]], "an off-state must not re-send every tick")

    def test_light_unavailable_at_first_is_turned_off_once_it_returns(self) -> None:
        """The regression this fixes.

        The screen went to sleep while every bulb was momentarily unavailable.
        The old code logged "turning lights off", sent nothing because the
        available list was empty, and latched a flag that stopped it ever
        retrying -- so the lights stayed on all night.
        """

        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, [], confirmed, 0.0)
        self.assertEqual(client.calls, [], "nothing to send while every light is unavailable")

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(client.calls, [["a", "b"]], "returning lights must still be turned off")
        self.assertEqual(confirmed, {"a", "b"})

    def test_lights_that_failed_to_turn_off_are_retried_on_the_next_sweep(self) -> None:
        client = FakeClient(failing={"b"})
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(confirmed, {"a"}, "a failed light must not count as confirmed off")

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(client.calls, [["a", "b"], ["b"]], "only the failed light is retried")


if __name__ == "__main__":
    unittest.main()
