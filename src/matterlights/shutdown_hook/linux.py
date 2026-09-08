"""Linux session-end detection: SIGTERM and SIGINT.

systemd sends ``SIGTERM`` and waits ``TimeoutStopSec`` for the process to exit,
so this covers ``systemctl --user stop matterlights-sync``, and -- because the
unit is ``PartOf=graphical-session.target`` -- an ordinary logout too. Ctrl-C on
a manual run arrives as ``SIGINT`` and is handled the same way.

It does NOT cover a shutdown or reboot: systemd stops the whole user manager, and
by then the session's D-Bus and network are going away. That is what the
system-level ``matterlights-lights-off.service`` is for, and why its ``ExecStop``
is ordered after ``network-online.target`` -- units stop in reverse dependency
order, so it runs while the network still works.

⚠️ **The previous handler is chained, never swallowed.** Replacing Python's
default SIGINT handler without calling on to it would leave a process that cannot
be interrupted with Ctrl-C, which is a genuinely hostile thing to do to whoever
is debugging at 2am.

⚠️ **Signal handlers can only be installed from the main thread.** Called from
anywhere else, ``signal.signal`` raises ``ValueError``; the factory degrades to a
no-op hook rather than taking the process down over it.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Callable

LOGGER = logging.getLogger("matterlights.shutdown_hook.linux")

_SESSION_END_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class SignalShutdownHook:
    def __init__(self, on_session_end: Callable[[], None], logger: logging.Logger | None = None) -> None:
        self._on_session_end = on_session_end
        self._logger = logger or LOGGER
        self._fired = threading.Lock()
        self._has_fired = False
        self._callback_thread: threading.Thread | None = None
        self._previous: dict[int, object] = {}

    def start(self) -> bool:
        try:
            for signal_number in _SESSION_END_SIGNALS:
                self._previous[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle)
        except ValueError:
            # Not the main thread. Nothing to clean up: signal.signal either
            # installed a handler or raised before doing anything.
            self._logger.debug("Signal handlers need the main thread", exc_info=True)
            return False
        return True

    def stop(self) -> None:
        for signal_number, previous in self._previous.items():
            try:
                signal.signal(signal_number, previous)
            except (ValueError, TypeError):
                pass
        self._previous.clear()

    def rearm(self) -> None:
        with self._fired:
            if not self._has_fired:
                return
            self._has_fired = False
        self._logger.info("Re-arming session-end detection")

    def fire(self, reason: str) -> None:
        """Start the callback at most once, whichever signal arrives first."""

        with self._fired:
            if self._has_fired:
                return
            self._has_fired = True

        self._logger.info("Session ending (%s); turning lights off", reason)
        worker = threading.Thread(target=self._run_callback, name="session-end", daemon=True)
        self._callback_thread = worker
        worker.start()

    def wait_for_callback(self, timeout: float | None = None) -> None:
        """Join the callback thread. For tests, which assert on its effects."""

        worker = self._callback_thread
        if worker is not None:
            worker.join(timeout)

    def _run_callback(self) -> None:
        try:
            self._on_session_end()
        except Exception:  # noqa: BLE001
            self._logger.exception("Failed to turn lights off during shutdown")

    def _handle(self, signal_number: int, frame) -> None:
        self.fire(signal.Signals(signal_number).name)

        # Chain to whatever was there before so the process still stops. Turning
        # the lights off must not turn the program into one that ignores Ctrl-C.
        previous = self._previous.get(signal_number)
        if callable(previous):
            previous(signal_number, frame)
        elif previous == signal.SIG_DFL:
            # Give the turn-off request a moment to reach Home Assistant, then
            # let the default action happen. systemd's TimeoutStopSec is the real
            # bound on this; the wait is deliberately shorter than any of them.
            self.wait_for_callback(timeout=5.0)
            signal.signal(signal_number, signal.SIG_DFL)
            signal.raise_signal(signal_number)
