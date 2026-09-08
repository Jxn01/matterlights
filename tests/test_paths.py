"""Per-OS state directories."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from matterlights import paths


class StateDirTest(unittest.TestCase):
    def test_localappdata_wins_when_set(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\jxnpe\AppData\Local"}, clear=True):
            self.assertEqual(Path(r"C:\Users\jxnpe\AppData\Local") / "matterlights", paths.state_dir())

    def test_xdg_state_home_is_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdgstate"}, clear=True):
            self.assertEqual(Path("/tmp/xdgstate/matterlights"), paths.state_dir())

    def test_defaults_to_local_state_not_the_working_directory(self) -> None:
        """The old fallback was cwd, which dropped a log file into the repo."""

        with mock.patch.dict(os.environ, {}, clear=True):
            resolved = paths.state_dir()
        self.assertEqual(Path.home() / ".local" / "state" / "matterlights", resolved)
        self.assertNotEqual(Path.cwd(), resolved.parent)

    def test_log_and_token_live_under_the_state_dir(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdgstate"}, clear=True):
            self.assertEqual(paths.state_dir(), paths.default_log_path().parent)
            self.assertEqual(paths.state_dir(), paths.portal_token_path().parent)
            self.assertEqual("matterlights.log", paths.default_log_path().name)


if __name__ == "__main__":
    unittest.main()


class LogPathDisableTest(unittest.TestCase):
    """LOG_PATH can be explicitly turned off, not just left unset.

    An EMPTY value cannot mean this: load_settings treats empty as unset and
    falls through to the per-OS default. The system-level shutdown unit needs a
    real way to say "journal only", or root writes a second copy of the log to
    /root where nobody will look for it.
    """

    def test_none_off_and_dash_all_disable_file_logging(self) -> None:
        from matterlights.config import _parse_log_path

        for value in ("none", "NONE", "off", "-", " none "):
            with self.subTest(value=value):
                self.assertIsNone(_parse_log_path(value, Path("/tmp")))

    def test_empty_also_disables(self) -> None:
        from matterlights.config import _parse_log_path

        self.assertIsNone(_parse_log_path("   ", Path("/tmp")))

    def test_a_real_path_still_works(self) -> None:
        from matterlights.config import _parse_log_path

        self.assertEqual(Path("/var/log/x.log"), _parse_log_path("/var/log/x.log", Path("/tmp")))

    def test_a_relative_path_resolves_against_the_base_dir(self) -> None:
        from matterlights.config import _parse_log_path

        self.assertEqual(Path("/tmp/x.log"), _parse_log_path("x.log", Path("/tmp")))
