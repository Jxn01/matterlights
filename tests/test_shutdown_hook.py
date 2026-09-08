from __future__ import annotations

import sys
import threading
import time
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

            # The query is acted on, not just acknowledged: WM_ENDSESSION often
            # never arrives before the process is killed. It must still return 1
            # so we never block the shutdown.
            self.assertEqual(_send(hook.hwnd, WM_QUERYENDSESSION, 0), 1)
            self.assertTrue(fired.wait(2.0), "callback did not run on WM_QUERYENDSESSION")

            # Every other path may fire too; the callback must still run once.
            _send(hook.hwnd, WM_ENDSESSION, 1)
            hook.fire("duplicate")
            hook.wait_for_callback(2.0)
            self.assertEqual(len(calls), 1)
        finally:
            hook.stop()

    def test_end_session_turns_the_lights_off_when_no_query_arrives(self) -> None:
        fired = threading.Event()
        hook = start_shutdown_hook(lambda: fired.set())
        try:
            _send(hook.hwnd, WM_ENDSESSION, 1)
            self.assertTrue(fired.wait(2.0), "callback did not run on WM_ENDSESSION")
        finally:
            hook.stop()

    def test_cancelled_shutdown_rearms_the_hook(self) -> None:
        calls: list[int] = []
        hook = start_shutdown_hook(lambda: calls.append(1))
        try:
            _send(hook.hwnd, WM_QUERYENDSESSION, 0)
            hook.wait_for_callback(2.0)
            self.assertEqual(len(calls), 1)

            # wParam FALSE means the session is not ending after all. Without
            # re-arming, this one cancelled shutdown would disable the hook for
            # the life of the process and the next real shutdown would be missed.
            _send(hook.hwnd, WM_ENDSESSION, 0)
            self.assertEqual(len(calls), 1, "re-arming must not itself fire the callback")

            _send(hook.hwnd, WM_QUERYENDSESSION, 0)
            hook.wait_for_callback(2.0)
            self.assertEqual(len(calls), 2, "hook did not re-arm after a cancelled shutdown")
        finally:
            hook.stop()

    def test_the_window_answers_immediately_even_when_the_callback_is_slow(self) -> None:
        """Windows waits for the WM_QUERYENDSESSION reply.

        An app that does not answer within HungAppTimeout (~5s) gets the "this
        app is preventing you from restarting" screen, so the turn-off must not
        run inline -- it has been measured taking over 3s under contention.
        """

        release = threading.Event()
        hook = start_shutdown_hook(lambda: release.wait(5.0))
        try:
            started = time.monotonic()
            self.assertEqual(_send(hook.hwnd, WM_QUERYENDSESSION, 0), 1)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0, f"window procedure blocked for {elapsed:.2f}s")
        finally:
            release.set()
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
                # The callback runs on its own thread, so the log record is not
                # emitted until it finishes.
                hook.wait_for_callback(2.0)
        finally:
            hook.stop()


if __name__ == "__main__":
    unittest.main()
