from __future__ import annotations

import sys
import threading
import unittest

from matterlights.shutdown_hook import start_shutdown_hook


WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016


def _send(hwnd: int, message: int, wparam: int) -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    return user32.SendMessageW(hwnd, message, wparam, 0)


@unittest.skipUnless(sys.platform == "win32", "session-end detection is Windows-only")
class ShutdownHookTests(unittest.TestCase):
    def test_end_session_turns_the_lights_off_exactly_once(self) -> None:
        calls: list[int] = []
        fired = threading.Event()

        def on_session_end() -> None:
            calls.append(1)
            fired.set()

        hook = start_shutdown_hook(on_session_end)
        try:
            self.assertIsNotNone(hook.hwnd, "hook did not create its window")

            # A query alone must not act: another app can still cancel shutdown.
            self.assertEqual(_send(hook.hwnd, WM_QUERYENDSESSION, 0), 1)
            self.assertFalse(fired.is_set())

            _send(hook.hwnd, WM_ENDSESSION, 1)
            self.assertTrue(fired.wait(2.0), "callback did not run on WM_ENDSESSION")

            # Both detection paths may fire; the callback must still run once.
            _send(hook.hwnd, WM_ENDSESSION, 1)
            hook.fire("duplicate")
            self.assertEqual(len(calls), 1)
        finally:
            hook.stop()

    def test_end_session_with_false_wparam_is_ignored(self) -> None:
        fired = threading.Event()
        hook = start_shutdown_hook(lambda: fired.set())
        try:
            # wParam FALSE means the session is *not* ending after all.
            _send(hook.hwnd, WM_ENDSESSION, 0)
            self.assertFalse(fired.wait(0.5))
        finally:
            hook.stop()

    def test_callback_errors_do_not_escape(self) -> None:
        def boom() -> None:
            raise RuntimeError("home assistant unreachable")

        hook = start_shutdown_hook(boom)
        try:
            with self.assertLogs("matterlights.shutdown_hook", level="ERROR"):
                _send(hook.hwnd, WM_ENDSESSION, 1)
        finally:
            hook.stop()


if __name__ == "__main__":
    unittest.main()
