"""Per-OS state directories."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
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
        self.assertEqual("matterlights", resolved.name)
        self.assertNotEqual(Path.cwd(), resolved.parent)

    @unittest.skipIf(
        sys.platform == "win32",
        "POSIX resolves home from the passwd database; Windows has only the environment",
    )
    def test_posix_defaults_under_the_home_directory(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            resolved = paths.state_dir()
        self.assertEqual(Path.home() / ".local" / "state" / "matterlights", resolved)

    def test_log_and_token_live_under_the_state_dir(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdgstate"}, clear=True):
            self.assertEqual(paths.state_dir(), paths.default_log_path().parent)
            self.assertEqual(paths.state_dir(), paths.portal_token_path().parent)
            self.assertEqual("matterlights.log", paths.default_log_path().name)


class HomeUndeterminableTest(unittest.TestCase):
    """Resolving the home directory must degrade, never raise.

    ⚠️ THIS ASYMMETRY IS WHY THE BUG WAS INVISIBLE ON LINUX. ``Path.home()`` is
    ``Path("~").expanduser()``. On POSIX that falls back to the passwd database
    when HOME is unset, so it cannot fail. On Windows it is purely
    environmental -- USERPROFILE, or HOMEDRIVE+HOMEPATH -- and with none of them
    set it raises ``RuntimeError("Could not determine home directory.")``.

    It matters because ``load_settings()`` evaluates the default log path
    EAGERLY on every call (it is an argument expression, so Python computes it
    even when LOG_PATH is set). A raise here is therefore not a bad log
    location, it is a startup crash for every entry point -- including
    ``lights_off`` under the SYSTEM shutdown task, whose entire job is to run
    when the machine is going down.

    These tests force the raising behaviour on EVERY platform so the guard
    cannot be regressed from the Linux side, where it is unreachable naturally.
    """

    def _home_raises(self):
        return mock.patch.object(
            Path, "home", side_effect=RuntimeError("Could not determine home directory.")
        )

    def test_state_dir_falls_back_instead_of_raising(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self._home_raises():
            resolved = paths.state_dir()

        self.assertEqual("matterlights", resolved.name)
        self.assertEqual(Path(tempfile.gettempdir()) / "matterlights", resolved)

    def test_default_log_path_survives_an_unresolvable_home(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self._home_raises():
            self.assertEqual("matterlights.log", paths.default_log_path().name)

    def test_localappdata_still_wins_over_the_fallback(self) -> None:
        """The fallback must not shadow a perfectly good LOCALAPPDATA."""

        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\jxnpe\AppData\Local"}, clear=True), \
                self._home_raises():
            self.assertEqual(
                Path(r"C:\Users\jxnpe\AppData\Local") / "matterlights", paths.state_dir()
            )

    def test_expand_user_returns_the_path_unchanged_when_home_is_unknown(self) -> None:
        """A '~' the OS cannot expand must not crash the caller.

        ``LOG_PATH=~/x.log`` in a .env is user input, and the three places that
        expand it all sit inside ``load_settings()``.
        """

        with mock.patch.object(
            Path, "expanduser", side_effect=RuntimeError("Could not determine home directory.")
        ):
            self.assertEqual(Path("~/logs/x.log"), paths.expand_user(Path("~/logs/x.log")))

    def test_expand_user_still_expands_normally(self) -> None:
        self.assertEqual(Path.home() / "x.log", paths.expand_user(Path("~/x.log")))

    def test_load_settings_survives_an_unresolvable_home(self) -> None:
        """The whole point: startup must not die on a degenerate environment."""

        from matterlights.config import load_settings

        env = {
            "MATTERLIGHTS_ENV_FILE": str(Path(tempfile.gettempdir()) / "does-not-exist.env"),
            "HA_URL": "http://127.0.0.1:8123",
            "HA_TOKEN": "token",
            "HA_LIGHT_ENTITIES": "light.one",
        }
        with mock.patch.dict(os.environ, env, clear=True), self._home_raises():
            settings = load_settings()

        self.assertEqual("matterlights.log", settings.log_path.name)


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


if __name__ == "__main__":
    unittest.main()
