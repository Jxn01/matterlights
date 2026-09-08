"""Every module in the package must import on the platform it claims to support.

This is the guard for a whole CLASS of bug, not one instance of it. Until
2026-09-08 ``screen.py`` began with ``from ctypes import windll`` at module
scope, so importing *anything* that reached it -- ``home_assistant``,
``playback``, ``main``, ``dashboard`` -- raised ``ImportError`` on Linux. The
package was not "mostly portable with a few Windows bits"; it was entirely
unusable off Windows, and nothing in the build said so.

A platform-specific import at module scope is invisible on the platform it was
written on. Only the other platform notices, and only at runtime. This test is
the thing that notices.

Modules named ``windows.py`` are expected to import only on win32 and
``linux.py`` only elsewhere -- they are the deliberate homes for platform code,
which is exactly why every OTHER module has no excuse.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import unittest

import matterlights


class ImportSurfaceTest(unittest.TestCase):
    def test_every_module_imports_on_this_platform(self) -> None:
        failures: list[str] = []

        for module_info in pkgutil.walk_packages(matterlights.__path__, "matterlights."):
            name = module_info.name
            leaf = name.rsplit(".", 1)[-1]
            if leaf == "windows" and sys.platform != "win32":
                continue
            if leaf == "linux" and sys.platform == "win32":
                continue
            try:
                importlib.import_module(name)
            except Exception as error:  # noqa: BLE001 - any failure is the finding
                failures.append(f"{name}: {error!r}")

        self.assertEqual(
            [],
            failures,
            "modules failed to import on "
            f"{sys.platform}; a platform-specific import at module scope is the usual cause",
        )


if __name__ == "__main__":
    unittest.main()
