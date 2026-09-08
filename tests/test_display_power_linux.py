"""Linux display-sleep detection."""

from __future__ import annotations

import sys
import unittest

if sys.platform == "win32":  # pragma: no cover
    raise unittest.SkipTest("Linux display power")

from matterlights.display_power import AlwaysOnDisplayMonitor, start_display_monitor
from matterlights.display_power.linux import (
    display_on_from_dpms,
    display_on_from_power_save_mode,
)


class PowerSaveModeTest(unittest.TestCase):
    def test_zero_is_on(self) -> None:
        self.assertTrue(display_on_from_power_save_mode(0))

    def test_standby_suspend_and_off_are_all_off(self) -> None:
        for value in (1, 2, 3):
            with self.subTest(power_save_mode=value):
                self.assertFalse(display_on_from_power_save_mode(value))

    def test_unknown_is_treated_as_ON(self) -> None:
        """-1 means Mutter does not know. Acting on that would be a fault.

        Reading "unknown" as "off" turns the lights out in a room someone is
        sitting in. Every failure path in this module biases towards on.
        """

        self.assertTrue(display_on_from_power_save_mode(-1))


class DpmsFallbackTest(unittest.TestCase):
    def test_any_output_on_means_on(self) -> None:
        """Three monitors, one asleep, does not mean the user left."""

        self.assertTrue(display_on_from_dpms(["Off", "On", "Off"]))

    def test_all_off_means_off(self) -> None:
        self.assertFalse(display_on_from_dpms(["Off", "Off"]))

    def test_no_readable_outputs_means_on(self) -> None:
        self.assertTrue(display_on_from_dpms([]))

    def test_values_are_matched_case_insensitively_and_trimmed(self) -> None:
        self.assertTrue(display_on_from_dpms(["on\n"]))


class FactoryTest(unittest.TestCase):
    def test_returns_something_usable_on_this_machine(self) -> None:
        monitor = start_display_monitor()
        self.addCleanup(monitor.stop)
        self.assertTrue(hasattr(monitor, "is_display_on"))
        self.assertIsInstance(monitor.is_display_on(), bool)

    def test_always_on_monitor_is_the_safe_default(self) -> None:
        self.assertTrue(AlwaysOnDisplayMonitor().is_display_on())


if __name__ == "__main__":
    unittest.main()
