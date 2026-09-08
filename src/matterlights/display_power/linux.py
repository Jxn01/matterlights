"""Linux display-power detection.

Primary source is GNOME's ``org.gnome.Mutter.DisplayConfig.PowerSaveMode``,
watched via ``PropertiesChanged`` so the state converges without polling. The
DPMS values are the X11 ones Mutter inherited:

    0 = on, 1 = standby, 2 = suspend, 3 = off, -1 = unknown

⚠️ **-1 is treated as ON.** "Unknown" is not "off", and the two must not be
collapsed: reading an unknown state as off turns the lights out in an occupied
room. The whole module is biased that way -- every failure path reports the
display as on.

The fallback reads ``/sys/class/drm/*/dpms``, which needs no compositor at all
and works on any KMS system, at the cost of polling.
"""

from __future__ import annotations

import glob
import logging
import threading

LOGGER = logging.getLogger("matterlights.display_power.linux")

DISPLAY_CONFIG_NAME = "org.gnome.Mutter.DisplayConfig"
DISPLAY_CONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"

_POWER_SAVE_ON = 0
_POWER_SAVE_UNKNOWN = -1

_DPMS_POLL_SECONDS = 5.0


def display_on_from_power_save_mode(value: int) -> bool:
    """Map a Mutter ``PowerSaveMode`` to on/off.

    Only an explicit 0 means on -- except that -1 (unknown) also means on,
    because acting on ignorance is how the lights get turned off in someone's
    face. See the module docstring.
    """

    if value == _POWER_SAVE_UNKNOWN:
        return True
    return value == _POWER_SAVE_ON


def display_on_from_dpms(states: list[str]) -> bool:
    """True if ANY connected output reports DPMS ``On``.

    Any, not all: with three monitors, one going to sleep on its own does not
    mean the user has left.
    """

    if not states:
        return True
    return any(state.strip().lower() == "on" for state in states)


def read_dpms_states() -> list[str]:
    states: list[str] = []
    for dpms_path in sorted(glob.glob("/sys/class/drm/card*-*/dpms")):
        status_path = dpms_path.rsplit("/", 1)[0] + "/status"
        try:
            with open(status_path, encoding="ascii") as handle:
                if handle.read().strip() != "connected":
                    continue
            with open(dpms_path, encoding="ascii") as handle:
                states.append(handle.read())
        except OSError:
            continue
    return states


class LinuxDisplayMonitor:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        self._on_event = threading.Event()
        self._on_event.set()  # Assume on until told otherwise.
        self._stop = threading.Event()
        self._subscription: int | None = None
        self._bus = None
        self._poller: threading.Thread | None = None

    def start(self) -> bool:
        if self._start_mutter():
            return True
        return self._start_dpms_polling()

    def is_display_on(self) -> bool:
        return self._on_event.is_set()

    def stop(self) -> None:
        self._stop.set()
        if self._subscription is not None and self._bus is not None:
            try:
                self._bus.signal_unsubscribe(self._subscription)
            except Exception:  # noqa: BLE001
                pass
            self._subscription = None

    # -- Mutter --------------------------------------------------------------

    def _start_mutter(self) -> bool:
        try:
            from matterlights.capture.mutter import ensure_main_loop
            from gi.repository import Gio, GLib

            ensure_main_loop()
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            current = self._bus.call_sync(
                DISPLAY_CONFIG_NAME,
                DISPLAY_CONFIG_PATH,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", (DISPLAY_CONFIG_NAME, "PowerSaveMode")),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            ).unpack()[0]
            self._apply(int(current))

            def on_properties_changed(_c, _s, _p, _i, _sig, params):
                _iface, changed, _invalidated = params.unpack()
                if "PowerSaveMode" in changed:
                    self._apply(int(changed["PowerSaveMode"]))

            self._subscription = self._bus.signal_subscribe(
                None,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                DISPLAY_CONFIG_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                on_properties_changed,
            )
            self._logger.info("Display power: watching Mutter PowerSaveMode")
            return True
        except Exception:  # noqa: BLE001 - not GNOME, or no session bus
            self._logger.debug("Mutter PowerSaveMode unavailable", exc_info=True)
            return False

    # -- sysfs DPMS ----------------------------------------------------------

    def _start_dpms_polling(self) -> bool:
        states = read_dpms_states()
        if not states:
            return False
        self._apply_dpms(states)
        self._poller = threading.Thread(target=self._poll_dpms, name="dpms-poll", daemon=True)
        self._poller.start()
        self._logger.info("Display power: polling /sys/class/drm DPMS")
        return True

    def _poll_dpms(self) -> None:
        while not self._stop.wait(_DPMS_POLL_SECONDS):
            self._apply_dpms(read_dpms_states())

    def _apply_dpms(self, states: list[str]) -> None:
        self._set(display_on_from_dpms(states))

    def _apply(self, power_save_mode: int) -> None:
        self._set(display_on_from_power_save_mode(power_save_mode))

    def _set(self, display_on: bool) -> None:
        if display_on == self._on_event.is_set():
            return
        if display_on:
            self._on_event.set()
        else:
            self._on_event.clear()
        self._logger.info("Display %s", "on" if display_on else "off")
