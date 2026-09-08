"""Turn the lights off when the session ends.

A background process is simply killed at shutdown, so the lights would otherwise
stay on at whatever colour they last had. Both platforms get told first -- and on
both, **the in-process hook is the least reliable of the layers, not the main
one.**

THE SAME LESSON ON BOTH SYSTEMS. On Windows, measurement showed that only a task
running as SYSTEM in session 0 actually fires at shutdown: by the time the
session-end event is written, the interactive session is already being destroyed
and Windows will not start a process in it. The Linux mirror is exact -- a
**user** unit dies with the session, so the layer that must survive a shutdown is
a **system** unit. Same conclusion, same reason: the thing being torn down cannot
be the thing that reports the teardown.

So the three layers are:

===============  ==========================================  ==========================
Layer            Mechanism                                   Covers
===============  ==========================================  ==========================
In-process       ``WM_QUERYENDSESSION`` / ``SIGTERM``         logoff, service stop, Ctrl-C
System-level     Task Scheduler on event 1074 / an            shutdown, reboot
                 ``ExecStop=`` in a system unit
Home Assistant   heartbeat staleness automation              crash, hard reset, power cut
===============  ==========================================  ==========================

On Linux the in-process layer is considerably more trustworthy than on Windows,
because ``systemctl stop`` and a logout that stops a ``PartOf=`` unit both send a
real ``SIGTERM`` and wait for it. It still does not cover a shutdown, because
systemd stops the user manager as a whole.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Protocol

LOGGER = logging.getLogger("matterlights.shutdown_hook")


class ShutdownHook(Protocol):
    def stop(self) -> None: ...

    def rearm(self) -> None: ...


class _NullHook:
    def stop(self) -> None:
        return None

    def rearm(self) -> None:
        return None


def start_shutdown_hook(
    on_session_end: Callable[[], None],
    logger: logging.Logger | None = None,
) -> ShutdownHook:
    """Call ``on_session_end`` once when the session is ending.

    Falls back to a no-op hook on any platform or failure, so the sync loop
    always keeps running -- losing the turn-off is much better than losing the
    program.
    """

    log = logger or LOGGER

    try:
        if sys.platform == "win32":
            from matterlights.shutdown_hook.windows import Win32ShutdownHook

            hook = Win32ShutdownHook(on_session_end, log)
        else:
            from matterlights.shutdown_hook.linux import SignalShutdownHook

            hook = SignalShutdownHook(on_session_end, log)
    except Exception:  # noqa: BLE001
        log.warning("Shutdown detection unavailable; lights will not turn off at shutdown.", exc_info=True)
        return _NullHook()

    if not hook.start():
        log.warning("Shutdown detection unavailable; lights will not turn off at shutdown.")
        return _NullHook()
    return hook
