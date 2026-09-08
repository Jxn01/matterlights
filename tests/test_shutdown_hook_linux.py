"""Linux session-end detection: SIGTERM and SIGINT."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import unittest

if sys.platform == "win32":  # pragma: no cover
    raise unittest.SkipTest("POSIX signal hook")

from matterlights.shutdown_hook.linux import SignalShutdownHook


class SignalHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[int] = []
        self.done = threading.Event()
        self.logger = logging.getLogger("test.shutdown")

    def _hook(self) -> SignalShutdownHook:
        def on_end() -> None:
            self.calls.append(1)
            self.done.set()

        hook = SignalShutdownHook(on_end, self.logger)
        self.addCleanup(hook.stop)
        return hook

    def test_fire_runs_the_callback(self) -> None:
        hook = self._hook()
        hook.fire("SIGTERM")
        hook.wait_for_callback(2.0)
        self.assertEqual([1], self.calls)

    def test_fire_runs_at_most_once(self) -> None:
        """Both signals may arrive; the lights are turned off once."""

        hook = self._hook()
        hook.fire("SIGTERM")
        hook.fire("SIGINT")
        hook.wait_for_callback(2.0)
        self.assertEqual([1], self.calls)

    def test_rearm_allows_firing_again(self) -> None:
        hook = self._hook()
        hook.fire("SIGTERM")
        hook.wait_for_callback(2.0)
        hook.rearm()
        hook.fire("SIGTERM")
        hook.wait_for_callback(2.0)
        self.assertEqual([1, 1], self.calls)

    def test_a_failing_callback_does_not_escape(self) -> None:
        """Losing the turn-off is better than losing the program."""

        def boom() -> None:
            raise RuntimeError("Home Assistant unreachable")

        hook = SignalShutdownHook(boom, self.logger)
        self.addCleanup(hook.stop)
        hook.fire("SIGTERM")
        hook.wait_for_callback(2.0)

    def test_start_installs_and_stop_restores_handlers(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        hook = self._hook()
        self.assertTrue(hook.start())
        self.assertNotEqual(before, signal.getsignal(signal.SIGTERM))
        hook.stop()
        self.assertEqual(before, signal.getsignal(signal.SIGTERM))

    def test_a_real_sigterm_fires_the_hook(self) -> None:
        """Deliver through the kernel, so the signal plumbing is under test.

        A prior handler is installed first, deliberately. Without one the hook
        chains to SIG_DFL and re-raises -- which is exactly right in production,
        where systemd sends SIGTERM and expects the process to die, but would
        kill this test runner. That path is covered in a subprocess below.
        """

        signal.signal(signal.SIGTERM, lambda *_: None)
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        hook = self._hook()
        hook.start()
        signal.raise_signal(signal.SIGTERM)
        self.assertTrue(self.done.wait(2.0))
        self.assertEqual([1], self.calls)

    def test_sigterm_turns_the_lights_off_AND_still_exits(self) -> None:
        """The whole contract, in a real process that really gets SIGTERM.

        Turning the lights off must not turn MatterLights into a program that
        ignores SIGTERM: systemd would then wait out TimeoutStopSec and SIGKILL
        it at every logout and shutdown.
        """

        import os
        import subprocess
        import tempfile

        marker = tempfile.NamedTemporaryFile(delete=False)
        marker.close()
        self.addCleanup(os.unlink, marker.name)

        code = (
            "import os, signal, sys, time;"
            "from matterlights.shutdown_hook.linux import SignalShutdownHook;"
            f"hook = SignalShutdownHook(lambda: open({marker.name!r}, 'w').write('off'));"
            "hook.start();"
            "print('ready', flush=True);"
            "time.sleep(30)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
        )
        self.assertEqual("ready", child.stdout.readline().strip())
        child.send_signal(signal.SIGTERM)
        returncode = child.wait(timeout=15)

        with open(marker.name, encoding="utf-8") as handle:
            self.assertEqual("off", handle.read(), "the lights-off callback did not run")
        self.assertNotEqual(0, returncode, "the process must actually die on SIGTERM")

    def test_off_main_thread_start_returns_false_rather_than_raising(self) -> None:
        result: list[bool] = []

        def run() -> None:
            hook = SignalShutdownHook(lambda: None, self.logger)
            result.append(hook.start())

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5.0)
        self.assertEqual([False], result)


if __name__ == "__main__":
    unittest.main()
