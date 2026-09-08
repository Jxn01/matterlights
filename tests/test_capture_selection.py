"""R1: the portal is chosen by desktop identity, never by a Mutter error."""

from __future__ import annotations

import logging
import sys
import unittest
from unittest import mock

if sys.platform == "win32":  # pragma: no cover
    raise unittest.SkipTest("Linux capture backend")

from matterlights.capture import linux as linux_capture


class SourceSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test.selection")

    def test_mutter_is_used_when_its_bus_name_exists(self) -> None:
        with mock.patch("matterlights.capture.mutter.mutter_available", return_value=True), \
             mock.patch("matterlights.capture.mutter.MutterSource") as mutter:
            source = linux_capture.select_source(self.logger)
        self.assertIs(mutter.return_value, source)

    def test_a_mutter_failure_does_NOT_fall_back_to_the_portal(self) -> None:
        """The whole point of R1.

        A Mutter constructor that raises means GNOME answered badly, not that we
        are on KDE. Falling back here would raise a consent dialog at login that
        nobody is present to click, and the sync loop would hang on it.
        """

        with mock.patch("matterlights.capture.mutter.mutter_available", return_value=True), \
             mock.patch("matterlights.capture.mutter.MutterSource",
                        side_effect=RuntimeError("gnome-shell still starting")), \
             mock.patch("matterlights.capture.portal.PortalSource") as portal:
            with self.assertRaises(RuntimeError):
                linux_capture.select_source(self.logger)
        portal.assert_not_called()

    def test_portal_is_used_only_when_mutter_is_absent(self) -> None:
        with mock.patch("matterlights.capture.mutter.mutter_available", return_value=False), \
             mock.patch("matterlights.capture.portal.portal_available", return_value=True), \
             mock.patch("matterlights.capture.portal.PortalSource") as portal:
            source = linux_capture.select_source(self.logger)
        self.assertIs(portal.return_value, source)

    def test_no_backend_at_all_names_the_packages_to_install(self) -> None:
        with mock.patch("matterlights.capture.mutter.mutter_available", return_value=False), \
             mock.patch("matterlights.capture.portal.portal_available", return_value=False):
            with self.assertRaises(Exception) as caught:
                linux_capture.select_source(self.logger)
        self.assertIn("xdg-desktop-portal", str(caught.exception))


class PortalRequestPathTest(unittest.TestCase):
    """The Request path must be derivable before the call, or it is a race."""

    def test_unique_name_is_mangled_the_documented_way(self) -> None:
        from matterlights.capture.portal import _request_path

        self.assertEqual(
            "/org/freedesktop/portal/desktop/request/1_42/tok",
            _request_path(":1.42", "tok"),
        )


class PortalAsymmetryTest(unittest.TestCase):
    def test_portal_declares_that_the_user_picks_the_screen(self) -> None:
        """So the dashboard can say so rather than offer a dead control."""

        from matterlights.capture.portal import PortalSource

        self.assertTrue(PortalSource.target_is_user_selected)

    def test_mutter_does_not_claim_that(self) -> None:
        from matterlights.capture.mutter import MutterSource

        self.assertFalse(getattr(MutterSource, "target_is_user_selected", False))


if __name__ == "__main__":
    unittest.main()
