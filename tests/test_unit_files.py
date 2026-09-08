"""The shipped systemd units.

These assertions look pedantic and are not. Each one pins a property whose
absence produces a failure that is silent, intermittent, or both.
"""

from __future__ import annotations

import configparser
from pathlib import Path
import unittest

UNIT_DIR = Path(__file__).resolve().parents[1] / "systemd"


def read_unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    # systemd keys are case-sensitive; configparser lowercases them by default.
    parser.optionxform = str
    parser.read(UNIT_DIR / name)
    return parser


class UserUnitTest(unittest.TestCase):
    UNITS = ("matterlights-sync.service", "matterlights-dashboard.service")

    def test_units_exist(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                self.assertTrue((UNIT_DIR / name).is_file())

    def test_tied_to_the_graphical_session(self) -> None:
        """The lingering trap.

        Lingering is enabled on this machine, so a user unit on default.target
        survives logout: capture dead because the compositor is gone, lights
        stuck on their last colour. PartOf makes logout stop the unit, which
        sends SIGTERM, which turns the lights off.
        """

        for name in self.UNITS:
            with self.subTest(unit=name):
                unit = read_unit(name)
                self.assertEqual("graphical-session.target", unit["Unit"]["PartOf"])
                self.assertEqual("graphical-session.target", unit["Install"]["WantedBy"])

    def test_stop_timeout_exceeds_the_session_end_timeout(self) -> None:
        """SESSION_END_TIMEOUT_SECONDS defaults to 10s.

        A shorter TimeoutStopSec would have systemd SIGKILL the process while it
        was still waiting on Home Assistant, every single logout.
        """

        for name in self.UNITS:
            with self.subTest(unit=name):
                unit = read_unit(name)
                self.assertGreaterEqual(int(unit["Service"]["TimeoutStopSec"]), 15)
                self.assertEqual("SIGTERM", unit["Service"]["KillSignal"])

    def test_env_file_is_explicit(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                unit = read_unit(name)
                self.assertIn("MATTERLIGHTS_ENV_FILE", unit["Service"]["Environment"])


class SystemUnitTest(unittest.TestCase):
    NAME = "matterlights-lights-off.service"

    def setUp(self) -> None:
        self.unit = read_unit(self.NAME)

    def test_the_work_is_in_ExecStop(self) -> None:
        """Nothing to do at boot; the whole point is what happens at shutdown."""

        self.assertEqual("oneshot", self.unit["Service"]["Type"])
        self.assertEqual("yes", self.unit["Service"]["RemainAfterExit"])
        self.assertIn("matterlights.lights_off", self.unit["Service"]["ExecStop"])

    def test_ordered_so_it_stops_while_the_network_is_still_up(self) -> None:
        """systemd stops units in reverse order, so After= here means before."""

        self.assertIn("network-online.target", self.unit["Unit"]["After"])
        self.assertIn("network-online.target", self.unit["Unit"]["Wants"])

    def test_requires_the_mount_holding_dot_env(self) -> None:
        self.assertIn("RequiresMountsFor", self.unit["Unit"])

    def test_env_file_is_explicit_because_root_has_no_useful_cwd(self) -> None:
        """Runs as root with cwd=/, so relative .env discovery finds nothing."""

        self.assertIn("MATTERLIGHTS_ENV_FILE", self.unit["Service"]["Environment"])

    def test_installed_at_system_level(self) -> None:
        """A USER unit dies with the session and so cannot report the session
        dying -- the same reason the Windows task has to run as SYSTEM."""

        self.assertEqual("multi-user.target", self.unit["Install"]["WantedBy"])


class TemplatingTest(unittest.TestCase):
    def test_every_unit_uses_the_install_dir_placeholder(self) -> None:
        for path in UNIT_DIR.glob("*.service"):
            with self.subTest(unit=path.name):
                self.assertIn("@INSTALL_DIR@", path.read_text())

    def test_no_absolute_home_paths_leaked_into_the_units(self) -> None:
        for path in UNIT_DIR.glob("*.service"):
            with self.subTest(unit=path.name):
                self.assertNotIn("/home/", path.read_text())


if __name__ == "__main__":
    unittest.main()
