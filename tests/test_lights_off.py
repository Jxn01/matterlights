"""A shutdown must not darken the room when the sync loop never ran.

jxn-files powers Garuda on and off with nobody at it, and matterlights-lights-off.service fires at every shutdown. Without this the
owner's lights would go out whenever the file server finished with the machine.
"""
from __future__ import annotations

import configparser
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from matterlights import lights_off, sync_mark

ROOT = Path(__file__).resolve().parents[1]


class Mark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mark = Path(self.tmp.name) / "synced"
        patcher = mock.patch.object(sync_mark, "SYNC_MARK", self.mark)
        patcher.start()
        self.addCleanup(patcher.stop)
        platform = mock.patch.object(sync_mark.sys, "platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)
        self.settings = mock.Mock(light_entities=["light.a"], log_path=None)
        self.client = mock.Mock()
        for target, value in (("load_settings", mock.Mock(return_value=self.settings)), ("HomeAssistantClient", mock.Mock(return_value=self.client)),
                              ("configure_logging", mock.Mock())):
            patcher = mock.patch.object(lights_off, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_lights_off_skips_when_sync_never_ran(self):
        self.assertEqual(lights_off.main(), 0)
        self.assertEqual(self.client.mock_calls, [])

    def test_lights_off_acts_when_sync_ran(self):
        sync_mark.leave_mark()
        lights_off.main()
        self.assertNotEqual(self.client.mock_calls, [])

    def test_the_windows_half_is_unchanged(self):
        with mock.patch.object(sync_mark.sys, "platform", "win32"):
            lights_off.main()
        self.assertNotEqual(self.client.mock_calls, [])

    def test_a_mark_that_cannot_be_left_does_not_stop_the_sync(self):
        with mock.patch.object(sync_mark, "SYNC_MARK", Path(self.tmp.name) / "no/such/folder/synced"):
            sync_mark.leave_mark()

    def test_windows_leaves_no_mark(self):
        with mock.patch.object(sync_mark.sys, "platform", "win32"):
            sync_mark.leave_mark()
        self.assertFalse(self.mark.exists())


class Placement(unittest.TestCase):
    def test_the_mark_survives_the_user_session(self):
        """/run/user/1000 is torn down with the session, before this unit stops, so the mark cannot live there."""
        self.assertFalse(str(sync_mark.SYNC_MARK).startswith("/run/user"))
        self.assertEqual(str(sync_mark.SYNC_MARK), "/run/matterlights/synced")

    def test_the_unit_provides_the_folder(self):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read(ROOT / "systemd/matterlights-lights-off.service", encoding="utf-8")
        self.assertEqual(parser["Service"]["RuntimeDirectory"], "matterlights")
        self.assertEqual(parser["Service"]["RuntimeDirectoryMode"], "1777")

    def test_the_sync_loop_leaves_the_mark_once_it_holds_the_lock(self):
        source = (ROOT / "src/matterlights/main.py").read_text(encoding="utf-8")
        self.assertLess(source.index("acquire_sync_singleton(LOGGER)"), source.index("leave_mark()"))
