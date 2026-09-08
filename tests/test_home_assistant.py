from __future__ import annotations

import unittest
from unittest.mock import patch

from matterlights.home_assistant import HomeAssistantClient


class TurnOffLightsUrgentlyTests(unittest.TestCase):
    """The session-end path: one request, its own timeout, no retry."""

    def _client(self) -> HomeAssistantClient:
        return HomeAssistantClient("http://ha.invalid", "token", timeout_seconds=3.0)

    def test_uses_its_own_timeout_rather_than_the_sync_timeout(self) -> None:
        # The sync timeout is tuned for a loop that retries every tick. Session
        # end gets one attempt, and a grouped turn_off has been measured taking
        # over 3s when the Matter bridge is busy -- which left half the bulbs on.
        with patch.object(HomeAssistantClient, "_post_light_service", return_value=True) as post:
            self.assertTrue(self._client().turn_off_lights_urgently(["a", "b"], 10.0))

        self.assertEqual(post.call_args.kwargs["timeout_seconds"], 10.0)
        self.assertEqual(post.call_args.args[0], ("a", "b"))
        self.assertEqual(post.call_args.args[1], "turn_off")

    def test_sends_one_grouped_request_for_every_light(self) -> None:
        with patch.object(HomeAssistantClient, "_post_light_service", return_value=True) as post:
            self._client().turn_off_lights_urgently(["a", "b", "c"], 10.0)

        self.assertEqual(post.call_count, 1, "session end must not pay per-light round trips")

    def test_no_lights_is_a_no_op(self) -> None:
        with patch.object(HomeAssistantClient, "_post_light_service") as post:
            self.assertTrue(self._client().turn_off_lights_urgently([], 10.0))

        post.assert_not_called()

    def test_reports_failure_when_home_assistant_does_not_answer(self) -> None:
        with patch.object(HomeAssistantClient, "_post_light_service", return_value=False):
            self.assertFalse(self._client().turn_off_lights_urgently(["a"], 10.0))


if __name__ == "__main__":
    unittest.main()
