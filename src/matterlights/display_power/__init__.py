"""Detect when the display goes to sleep so the lights can follow it off.

Both platforms answer the same question -- "is the monitor powered on?" -- and
both answer it by subscribing to the compositor or OS rather than polling, so the
monitor converges within milliseconds of starting.

* Windows broadcasts ``GUID_CONSOLE_DISPLAY_STATE`` to a hidden message-only
  window (``windows.py``).
* GNOME exposes ``PowerSaveMode`` on ``org.gnome.Mutter.DisplayConfig``, with
  ``/sys/class/drm/*/dpms`` as a compositor-agnostic fallback (``linux.py``).

If anything about that setup fails -- wrong desktop, no D-Bus, an exception
during registration -- the factory returns an always-on monitor. That direction
is deliberate: an unknown display state must never be read as "off", because
that turns the lights off in a room where someone is sitting.
"""

from __future__ import annotations

import logging
import sys
from typing import Protocol

LOGGER = logging.getLogger("matterlights.display_power")


class DisplayMonitor(Protocol):
    def is_display_on(self) -> bool: ...

    def stop(self) -> None: ...


class AlwaysOnDisplayMonitor:
    """Fallback when display-power detection is unavailable.

    Reports the display as on, always. The sync loop then behaves exactly as it
    did before display-sleep support existed, which is the safe degradation:
    lights that stay on when they could have turned off are an annoyance, lights
    that turn off while you are using the machine are a fault.
    """

    def is_display_on(self) -> bool:
        return True

    def stop(self) -> None:
        return None


def start_display_monitor(logger: logging.Logger | None = None) -> DisplayMonitor:
    log = logger or LOGGER

    monitor = _platform_monitor(log)
    if monitor is None:
        return AlwaysOnDisplayMonitor()
    if not monitor.start():
        log.warning("Display power monitoring unavailable; lights will not follow screen sleep.")
        return AlwaysOnDisplayMonitor()
    return monitor


def _platform_monitor(log: logging.Logger):
    try:
        if sys.platform == "win32":
            from matterlights.display_power.windows import Win32DisplayMonitor

            return Win32DisplayMonitor(log)

        from matterlights.display_power.linux import LinuxDisplayMonitor

        return LinuxDisplayMonitor(log)
    except Exception:  # noqa: BLE001 - unsupported platform or missing bindings
        log.debug("No display power monitor for this platform", exc_info=True)
        return None
