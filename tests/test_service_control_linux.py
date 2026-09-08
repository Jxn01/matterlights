"""systemd service control.

The ``systemctl show`` outputs below were captured from real units on this
machine, not written by hand. That matters for one case in particular: a unit
that does not exist still exits 0 and still reports ``ActiveState=inactive``, so
"missing" can only be read out of ``LoadState``. A hand-written fixture would
very likely have guessed a non-zero exit code and hidden the bug.
"""

from __future__ import annotations

import os
import sys
import unittest

if sys.platform == "win32":  # pragma: no cover
    raise unittest.SkipTest("systemd service control")

from matterlights.service_control import DASHBOARD, SYNC, ZONE_UI, ServiceStatus
from matterlights.service_control.linux import (
    UNIT_NAMES,
    parse_systemctl_show,
    zone_ui_process_ids,
)

ACTIVE = """LoadState=loaded
ActiveState=active
SubState=running
Result=success
ExecMainStartTimestamp=Tue 2026-09-08 21:06:48 CEST
ExecMainExitTimestamp=
"""

NOT_FOUND = """LoadState=not-found
ActiveState=inactive
SubState=dead
Result=success
ExecMainStartTimestamp=
ExecMainExitTimestamp=
"""

FAILED = """LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
ExecMainStartTimestamp=Tue 2026-09-08 22:21:58 CEST
ExecMainExitTimestamp=Tue 2026-09-08 22:21:58 CEST
"""

INACTIVE = """LoadState=loaded
ActiveState=inactive
SubState=dead
Result=success
ExecMainStartTimestamp=Tue 2026-09-08 21:57:47 CEST
ExecMainExitTimestamp=Tue 2026-09-08 21:57:47 CEST
"""


class ParseSystemctlShowTest(unittest.TestCase):
    def test_active_unit(self) -> None:
        status = parse_systemctl_show(ACTIVE, "matterlights-sync.service")
        self.assertTrue(status.exists)
        self.assertEqual("active (running)", status.state)
        self.assertEqual("Tue 2026-09-08 21:06:48 CEST", status.last_run)
        self.assertEqual("success", status.last_result)

    def test_missing_unit_is_detected_from_LoadState_not_the_exit_code(self) -> None:
        """systemctl exits 0 for a unit that does not exist.

        It even reports ActiveState=inactive, which looks exactly like a stopped
        unit. LoadState is the only field that tells them apart.
        """

        status = parse_systemctl_show(NOT_FOUND, "matterlights-sync.service")
        self.assertFalse(status.exists)
        self.assertEqual("matterlights-sync.service", status.name)

    def test_inactive_but_installed_unit_still_exists(self) -> None:
        status = parse_systemctl_show(INACTIVE, "matterlights-dashboard.service")
        self.assertTrue(status.exists, "a stopped unit is installed, not missing")
        self.assertEqual("inactive (dead)", status.state)

    def test_failed_unit_reports_why(self) -> None:
        status = parse_systemctl_show(FAILED, "matterlights-sync.service")
        self.assertTrue(status.exists)
        self.assertEqual("failed", status.state)
        self.assertEqual("exit-code", status.last_result)

    def test_empty_timestamps_become_empty_strings(self) -> None:
        status = parse_systemctl_show(ACTIVE, "u.service")
        self.assertEqual("", status.next_run)

    def test_na_timestamps_are_normalised(self) -> None:
        """Older systemd writes 'n/a'; the dashboard should show a blank."""

        status = parse_systemctl_show(
            "LoadState=loaded\nActiveState=active\nSubState=running\nExecMainStartTimestamp=n/a\n",
            "u.service",
        )
        self.assertEqual("", status.last_run)


class ServiceStatusSerialisationTest(unittest.TestCase):
    def test_keys_match_what_the_dashboard_javascript_renders(self) -> None:
        payload = ServiceStatus(exists=True, name="u", state="active").as_dict()
        self.assertEqual(
            {"exists", "taskName", "state", "lastRunTime", "nextRunTime", "lastTaskResult"},
            set(payload),
        )


class UnitNamingTest(unittest.TestCase):
    def test_every_logical_service_maps_to_a_unit(self) -> None:
        for service in (SYNC, DASHBOARD, ZONE_UI):
            with self.subTest(service=service):
                self.assertTrue(UNIT_NAMES[service].endswith(".service"))


class ZoneUiProcessDiscoveryTest(unittest.TestCase):
    def test_does_not_match_its_own_process(self) -> None:
        """The bug ``pgrep -f`` would have.

        The searching process's own command line contains the pattern, so a
        substring match reports a phantom. This reads /proc/*/cmdline and
        compares argv tokens exactly, so the test process -- whose command line
        contains the module name via this very file -- must not appear.
        """

        self.assertNotIn(os.getpid(), zone_ui_process_ids())


if __name__ == "__main__":
    unittest.main()
