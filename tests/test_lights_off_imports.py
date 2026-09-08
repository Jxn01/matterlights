"""The shutdown helper must not drag in the capture stack. (R2)

``matterlights.lights_off`` runs from a SYSTEM unit, as root, while the machine
is shutting down, with no session bus. It exists to get exactly one HTTP request
to Home Assistant out before the power goes. Importing GStreamer and PyGObject
there costs import time it does not have, and ``gi`` with no session bus can fail
outright -- which would silently break the one layer that guarantees the lights
go off at shutdown.

This is easy to violate by accident and impossible to notice by hand: the chain
runs lights_off -> home_assistant -> screen (for RgbColor), and a single
module-scope ``from matterlights import capture`` in screen.py is enough. That
had in fact happened, and this test is what found it.

Run in a subprocess because the check is on a FRESH interpreter's sys.modules;
the test runner has already imported half the package.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

FORBIDDEN_ROOTS = ("gi", "mss")
FORBIDDEN_PREFIX = "matterlights.capture"


class LightsOffImportPurityTest(unittest.TestCase):
    def test_lights_off_does_not_import_the_capture_stack(self) -> None:
        code = (
            "import matterlights.lights_off, sys\n"
            f"roots = {FORBIDDEN_ROOTS!r}\n"
            "bad = sorted(m for m in sys.modules "
            f"if m.split('.')[0] in roots or m.startswith({FORBIDDEN_PREFIX!r}))\n"
            "print(','.join(bad))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        pulled_in = result.stdout.strip()
        self.assertEqual(
            "",
            pulled_in,
            "lights_off imported the capture stack: "
            f"{pulled_in}. Import it lazily inside the function that needs it "
            "-- see matterlights.screen._backend.",
        )

    def test_lights_off_still_imports_what_it_actually_needs(self) -> None:
        """Guard against 'fixing' the above by breaking the helper."""

        code = (
            "import matterlights.lights_off as m\n"
            "assert callable(m.main)\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual("ok", result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
