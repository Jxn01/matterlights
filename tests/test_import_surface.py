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

import ast
import importlib
from pathlib import Path
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



class HomeResolutionSurfaceTest(unittest.TestCase):
    """Home-directory resolution may only happen in ``paths.py``.

    THE SECOND CLASS OF PLATFORM BUG, and it hides the same way the first one
    did. ``Path.home()`` and ``Path.expanduser()`` CANNOT FAIL ON POSIX -- with
    no ``HOME`` set they fall back to the passwd database. On Windows they are
    purely environmental, and with no ``USERPROFILE`` (nor
    ``HOMEDRIVE``+``HOMEPATH``) they raise
    ``RuntimeError("Could not determine home directory.")``.

    So the exact same line is total-safe on the platform the port was developed
    on and a live crash path on the other one -- which is how an unguarded
    ``Path.home()`` reached ``paths.state_dir()``, a function ``load_settings()``
    evaluates EAGERLY on every single call. That made it a startup crash for
    every entry point, including ``lights_off`` under the SYSTEM shutdown task.

    ``paths.py`` owns the two guarded wrappers (``_home_or_none`` and
    ``expand_user``). Everywhere else must go through them, so the guarantee is
    structural rather than something each new call site has to remember.
    """

    _OWNER = "paths.py"

    def test_home_and_expanduser_are_confined_to_paths_py(self) -> None:
        offenders: list[str] = []
        package_root = Path(matterlights.__path__[0])

        for source in sorted(package_root.rglob("*.py")):
            if source.name == self._OWNER:
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                # ``anything.expanduser()`` -- always goes through paths.expand_user.
                if func.attr == "expanduser":
                    offenders.append(
                        f"{source.relative_to(package_root)}:{node.lineno} .expanduser()"
                    )
                # ``Path.home()`` specifically; an unrelated ``.home()`` is fine.
                elif (
                    func.attr == "home"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "Path"
                ):
                    offenders.append(
                        f"{source.relative_to(package_root)}:{node.lineno} Path.home()"
                    )

        self.assertEqual(
            [],
            offenders,
            "these call Path.home()/expanduser() directly, which raises on Windows when the "
            "environment has no USERPROFILE; use paths.expand_user() / paths.state_dir() instead",
        )


if __name__ == "__main__":
    unittest.main()
