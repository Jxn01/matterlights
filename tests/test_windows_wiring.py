"""Execute the Windows wiring on Linux, under a deliberately thin Win32 fake.

WHY THIS EXISTS. The Windows halves of this program cannot be run on the rig
that hosts them: Linux, Windows and Home Assistant OS are three boot options on
one machine and never run at the same time, so "boot Windows and try it" costs a
reboot and ends the session doing the testing. Before this file, the only
evidence the Windows side still worked after the Linux port was that it compiled.

Compiling is a weak claim. The port MOVED these modules -- capture came out of
``screen.py`` into ``capture/``, service control came out of ``dashboard.py``
behind a Protocol -- and the risk that creates is at the SEAMS: does the factory
still hand back the Windows class, does the Protocol still line up, do the JSON
keys the dashboard's JavaScript reads still come out with the same names.

Those seams are all plain Python. They do not need Windows; they need
``sys.platform`` to say ``win32`` and two ctypes attributes to exist. So they are
tested here, on Linux, on every run.

⚠️ **What this deliberately does NOT test.** The fake answers every Win32 call
with "1, fine". It cannot tell you that ``CreateWindowExW`` really produces a
window, that DXGI really produces a frame, or that Task Scheduler really accepts
a registration. That still requires Windows. What it tells you is that the
plumbing this port rearranged is connected -- which is exactly the part that
moved, and exactly the part a compile check cannot see.

The fake is kept thin on purpose. Growing it until the Win32 message loops run
would turn it into a simulator that mostly tests itself; the message-loop code is
verbatim from the pre-port commits and is guarded by the Windows-only tests that
run when the suite is executed on Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import sys
import unittest


_MATTERLIGHTS_PREFIXES = (
    "matterlights.capture",
    "matterlights.display_power",
    "matterlights.service_control",
    "matterlights.shutdown_hook",
    "matterlights.process_lock",
)


class _FakeFunction:
    """Any Win32 entry point. Returns 1, accepts argtypes/restype assignment."""

    argtypes: object = None
    restype: object = None

    def __call__(self, *_args, **_kwargs):
        return 1


class _FakeDll:
    def __getattr__(self, _name: str) -> _FakeFunction:
        return _FakeFunction()


class _FakeWindll:
    def __getattr__(self, _name: str) -> _FakeDll:
        return _FakeDll()


@contextlib.contextmanager
def win32_environment():
    """Make ``sys.platform == "win32"`` true enough to import the Windows modules.

    Only two ctypes attributes are genuinely missing on Linux: ``windll`` and
    ``WINFUNCTYPE``. ``ctypes.wintypes`` imports fine here, and ``mss`` is a
    cross-platform dependency that is already installed, so nothing else needs
    faking. ``WINFUNCTYPE`` maps to ``CFUNCTYPE`` because on x86-64 both are the
    same calling convention, so callback types still build.

    Everything is restored on exit, including ``sys.modules``. Without that the
    Windows classes would stay imported and leak into the rest of the suite,
    which runs in the same process.
    """

    saved_platform = sys.platform
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(_MATTERLIGHTS_PREFIXES)
    }
    had_windll = hasattr(ctypes, "windll")
    had_winfunctype = hasattr(ctypes, "WINFUNCTYPE")

    for name in list(saved_modules):
        del sys.modules[name]
    if not had_windll:
        ctypes.windll = _FakeWindll()
    if not had_winfunctype:
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    sys.platform = "win32"
    try:
        yield
    finally:
        sys.platform = saved_platform
        if not had_windll:
            del ctypes.windll
        if not had_winfunctype:
            del ctypes.WINFUNCTYPE
        for name in [n for n in sys.modules if n.startswith(_MATTERLIGHTS_PREFIXES)]:
            del sys.modules[name]
        sys.modules.update(saved_modules)


@unittest.skipIf(sys.platform == "win32", "on Windows the real tests run instead")
class WindowsModulesImportTest(unittest.TestCase):
    """Every Windows module must import. Four of them broke during the port."""

    def test_all_four_windows_modules_import(self) -> None:
        with win32_environment():
            for name in (
                "matterlights.capture.windows",
                "matterlights.display_power.windows",
                "matterlights.service_control.windows",
                "matterlights.shutdown_hook.windows",
            ):
                with self.subTest(module=name):
                    self.assertIsNotNone(importlib.import_module(name))

    def test_module_surgery_left_no_undefined_names(self) -> None:
        """Rebuilding these modules once stripped their constant blocks.

        Importing proves the module body executes; this additionally touches the
        module-level constants that the earlier surgery deleted, which import
        alone would not notice because they are only read inside functions.
        """

        with win32_environment():
            hook = importlib.import_module("matterlights.shutdown_hook.windows")
            for constant in (
                "_WM_QUERYENDSESSION",
                "_WM_ENDSESSION",
                "_SESSION_END_CTRL_EVENTS",
                "_WNDPROC",
            ):
                with self.subTest(constant=constant):
                    self.assertTrue(hasattr(hook, constant), constant)

            power = importlib.import_module("matterlights.display_power.windows")
            for constant in (
                "_WM_POWERBROADCAST",
                "_PBT_POWERSETTINGCHANGE",
                "_GUID_CONSOLE_DISPLAY_STATE",
            ):
                with self.subTest(constant=constant):
                    self.assertTrue(hasattr(power, constant), constant)


@unittest.skipIf(sys.platform == "win32", "on Windows the real tests run instead")
class FactoryDispatchTest(unittest.TestCase):
    """Each factory must hand back the Windows implementation -- and only it.

    The negative half matters as much as the positive one. A factory that fell
    through to the Linux class would still return *something*, and on Windows
    that something would fail at the first systemctl call rather than at import,
    which is a much worse place to find out.
    """

    def test_capture_backend_is_the_windows_one(self) -> None:
        with win32_environment():
            capture = importlib.import_module("matterlights.capture")
            backend = capture._create_backend()
            self.assertEqual(type(backend).__name__, "WindowsCaptureBackend")
            self.assertEqual(type(backend).__module__, "matterlights.capture.windows")

    def test_service_control_is_the_windows_one(self) -> None:
        with win32_environment():
            service_control = importlib.import_module("matterlights.service_control")
            control = service_control.get_service_control()
            self.assertEqual(type(control).__name__, "WindowsServiceControl")

    def test_no_factory_returns_a_linux_implementation(self) -> None:
        with win32_environment():
            capture = importlib.import_module("matterlights.capture")
            service_control = importlib.import_module("matterlights.service_control")
            for obj in (capture._create_backend(), service_control.get_service_control()):
                with self.subTest(obj=type(obj).__name__):
                    self.assertNotIn("linux", type(obj).__module__)
                    self.assertNotIn("Systemd", type(obj).__name__)
                    self.assertNotIn("Linux", type(obj).__name__)

    def test_linux_modules_are_not_even_imported_on_windows(self) -> None:
        """A stray import would drag GStreamer onto a machine that has none."""

        with win32_environment():
            importlib.import_module("matterlights.capture")._create_backend()
            importlib.import_module("matterlights.service_control").get_service_control()
            leaked = [
                name
                for name in sys.modules
                if name.startswith("matterlights.") and name.endswith(".linux")
            ]
            self.assertEqual(leaked, [], f"Linux modules imported under win32: {leaked}")


@unittest.skipIf(sys.platform == "win32", "on Windows the real tests run instead")
class DashboardContractTest(unittest.TestCase):
    """The dashboard's JavaScript reads these key names. They are a contract.

    Service control moved out of ``dashboard.py`` behind a Protocol during the
    port, and the claim made at the time was that the browser code needed no
    changes. That claim is only true while ``as_dict()`` keeps emitting the
    Windows Task Scheduler vocabulary, so it is asserted rather than remembered.
    """

    def test_status_dict_keeps_the_task_scheduler_key_names(self) -> None:
        with win32_environment():
            service_control = importlib.import_module("matterlights.service_control")
            status = service_control.ServiceStatus(
                exists=True,
                name="MatterLights Sync",
                state="Ready",
                last_run="2026-09-08",
                next_run="2026-09-09",
                last_result="0",
            )
            self.assertEqual(
                set(status.as_dict()),
                {"exists", "taskName", "state", "lastRunTime", "nextRunTime", "lastTaskResult"},
            )

    def test_the_dashboard_javascript_reads_exactly_those_keys(self) -> None:
        """Read the shipped HTML, so renaming a key in either place fails here."""

        from pathlib import Path

        dashboard = Path(__file__).resolve().parents[1] / "src" / "matterlights" / "dashboard.py"
        text = dashboard.read_text(encoding="utf-8")
        for key in ("taskName", "lastRunTime", "nextRunTime", "lastTaskResult"):
            with self.subTest(key=key):
                self.assertIn(key, text, f"dashboard no longer reads {key}")


@unittest.skipIf(sys.platform == "win32", "on Windows the real tests run instead")
class ShutdownHelperImportSurfaceTest(unittest.TestCase):
    """R2 holds on Windows too, not just on Linux.

    ``lights_off`` runs as a fresh process while the machine is already shutting
    down. Anything it imports is import time it does not have, and the capture
    stack is the expensive one. The Linux guard for this lives in
    ``test_lights_off_imports.py``; this is the same rule with the Windows
    modules in play, where the cost is DXGI rather than GStreamer.
    """

    def test_lights_off_does_not_drag_in_capture(self) -> None:
        with win32_environment():
            # Purge the capture dependencies as well, not just matterlights.
            # Earlier tests in this process import them legitimately, and a
            # leftover entry in sys.modules would make this assertion pass for
            # the wrong reason -- or fail for one, which is how it was caught.
            for name in [n for n in list(sys.modules) if n.startswith("matterlights")]:
                del sys.modules[name]
            saved_deps = {
                name: sys.modules.pop(name)
                for name in ("mss", "dxcam")
                if name in sys.modules
            }

            try:
                importlib.import_module("matterlights.lights_off")
                # Measure BEFORE restoring, or the restore is what we measure.
                forbidden = [
                    name
                    for name in sys.modules
                    if name.startswith("matterlights.capture") or name in ("mss", "dxcam")
                ]
            finally:
                sys.modules.update(
                    {k: v for k, v in saved_deps.items() if k not in sys.modules}
                )

            self.assertEqual(forbidden, [], f"shutdown helper imported: {forbidden}")

    def test_the_guard_can_actually_fail(self) -> None:
        """Prove the check above is capable of catching the thing it guards.

        R2 was violated once already and found by running this check, not by
        reading the code. A guard that cannot fail is decoration, so this
        deliberately imports the capture stack and asserts the same measurement
        turns red.
        """

        with win32_environment():
            importlib.import_module("matterlights.capture.windows")
            forbidden = [
                name
                for name in sys.modules
                if name.startswith("matterlights.capture") or name in ("mss", "dxcam")
            ]
            self.assertNotEqual(forbidden, [], "the R2 measurement cannot detect a violation")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
