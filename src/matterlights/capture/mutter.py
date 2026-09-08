"""Acquire a PipeWire node from GNOME's private ScreenCast API.

WHY NOT THE PORTAL. ``org.freedesktop.portal.ScreenCast`` is the standard,
cross-desktop route, and it is implemented here as ``portal.py``. But it raises a
**consent dialog**, and a screen-sync daemon that autostarts with the session
would meet that dialog at login with nobody looking at it. Mutter's own API --
the one the portal itself calls through to -- has no dialog, needs no restore
token to lose, and works headless from a systemd unit on first boot. Measured on
this rig: 14 ms to first frame, no prompt, clean teardown.

The cost is that this is a private, GNOME-only interface with no stability
promise (it is at version 4). ``portal.py`` is the hedge, and the selection rule
in ``capture/__init__.py`` decides between them by asking whether this is GNOME
at all -- never by catching an error from here. See R1 in the design doc.

GEOMETRY. Monitor sizes are reported in Mutter's LOGICAL coordinates -- mode size
divided by scale, axes swapped for a rotated output. Those are stage coordinates,
which is what ``RecordArea`` takes and what the dashboard needs to draw the
desktop arrangement correctly. They are deliberately NOT XWayland's numbers,
which report this rig's 3840x2160 primary as 7680x4320.
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import Callable

from matterlights.capture import CaptureUnavailable, Monitor

LOGGER = logging.getLogger("matterlights.capture.mutter")

SCREENCAST_NAME = "org.gnome.Mutter.ScreenCast"
SCREENCAST_PATH = "/org/gnome/Mutter/ScreenCast"
DISPLAY_CONFIG_NAME = "org.gnome.Mutter.DisplayConfig"
DISPLAY_CONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"

# Transforms 1/3/5/7 are the 90-degree rotations; they swap the axes.
_ROTATED_TRANSFORMS = frozenset({1, 3, 5, 7})

# CursorMode 0 = hidden. dxcam excludes the cursor on Windows, and sampling the
# pointer would let a moving mouse nudge the room's colour.
_CURSOR_MODE_HIDDEN = 0


class MutterUnavailable(RuntimeError):
    """Raised when this is not a GNOME session, or Mutter refused the request."""


# Substrings Mutter uses when it is refusing *for now* rather than failing.
# Matched case-insensitively against the D-Bus error message.
#
# "Session creation inhibited" is what Mutter answers while the display is
# asleep or the session is locked -- observed live on 2026-09-08 at 23:04:28,
# 687 ms after Mutter closed the previous session because the monitor was
# powering down and 7 ms before this program's own display-power watcher
# noticed. Both sides were behaving correctly; only the reporting was wrong.
_TRANSIENT_DBUS_ERROR_MARKERS = ("inhibited",)


def is_transient_dbus_error(error) -> bool:
    """Whether a ``GLib.Error`` means "not right now" rather than "broken".

    Pure and takes anything with a ``message``, so the classification can be
    tested without a session bus, a compositor, or a sleeping monitor.

    Kept deliberately narrow. A broad match here would swallow real faults --
    a missing interface, a permission denial, a Mutter that crashed -- and turn
    a loud failure into a silent retry loop that never recovers.
    """

    message = getattr(error, "message", None) or str(error)
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_DBUS_ERROR_MARKERS)


def _gi():
    """Import PyGObject, turning the failure into an actionable message.

    PyGObject and the GStreamer PipeWire plugin are SYSTEM packages, not pip
    ones. A venv built without ``--system-site-packages`` cannot see them and
    fails here with an opaque ImportError, so name the fix in the error.
    """

    try:
        from gi.repository import Gio, GLib

        return Gio, GLib
    except ImportError as error:  # pragma: no cover - environment dependent
        raise MutterUnavailable(
            "PyGObject is not importable. MatterLights needs the system packages "
            "'python-gobject', 'gstreamer', 'gst-plugins-base' and 'gst-plugin-pipewire', "
            "and a virtualenv created with --system-site-packages so it can see them. "
            f"Original error: {error}"
        ) from error


_LOOP_LOCK = threading.Lock()
_LOOP_STARTED = False


def ensure_main_loop() -> None:
    """Run one GLib main loop for the process, on a daemon thread.

    D-Bus signal delivery needs a main loop turning. GStreamer's ``new-sample``
    callback does not -- it fires on the streaming thread -- so this exists
    purely so ``PipeWireStreamAdded``, ``Closed`` and ``MonitorsChanged`` arrive.
    """

    global _LOOP_STARTED
    with _LOOP_LOCK:
        if _LOOP_STARTED:
            return
        _, GLib = _gi()
        loop = GLib.MainLoop()
        threading.Thread(target=loop.run, name="matterlights-glib", daemon=True).start()
        _LOOP_STARTED = True


def mutter_available() -> bool:
    """Is ``org.gnome.Mutter.ScreenCast`` a name on the session bus?

    This is the ONLY question allowed to select the portal backend instead. A
    Mutter call that *fails* means GNOME answered badly and should be retried;
    it does not mean we are on KDE. Collapsing those two states is how an
    autostarted daemon ends up hanging on a consent dialog at login.
    """

    try:
        Gio, GLib = _gi()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
            None,
            GLib.VariantType.new("(as)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        if SCREENCAST_NAME in reply.unpack()[0]:
            return True

        # Not currently running is not the same as not installed: the service is
        # D-Bus activatable and gnome-shell may simply not have been asked yet.
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListActivatableNames",
            None,
            GLib.VariantType.new("(as)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return SCREENCAST_NAME in reply.unpack()[0]
    except Exception:  # noqa: BLE001 - no bus, no GNOME
        LOGGER.debug("Mutter ScreenCast name lookup failed", exc_info=True)
        return False


class MutterSource:
    """Opens a ScreenCast session and hands back PipeWire node ids."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        Gio, GLib = _gi()
        self._Gio = Gio
        self._GLib = GLib
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._session_path: str | None = None
        self._stream_path: str | None = None
        self._subscriptions: list[int] = []
        self._on_invalidated: Callable[[str], None] | None = None
        self._closing = False
        self._last_logged_target: tuple | None = None
        self.active_connector: str = ""
        ensure_main_loop()
        atexit.register(self.close)

    # -- lifecycle ---------------------------------------------------------

    def set_invalidation_callback(self, callback: Callable[[str], None]) -> None:
        """Called with a reason when the session, stream or layout goes away."""

        self._on_invalidated = callback

    def close(self) -> None:
        # Unsubscribe BEFORE stopping, and latch a flag as well: Mutter emits
        # Stream.Closed in response to our own Stop(), and treating that as an
        # invalidation would schedule a rebuild of a session we just retired.
        self._closing = True
        for subscription in self._subscriptions:
            try:
                self._bus.signal_unsubscribe(subscription)
            except Exception:  # noqa: BLE001
                pass
        self._subscriptions.clear()
        if self._session_path is not None:
            try:
                self._call(self._session_path, f"{SCREENCAST_NAME}.Session", "Stop")
            except Exception:  # noqa: BLE001 - already gone is fine
                self._logger.debug("ScreenCast session already closed", exc_info=True)
        self._session_path = None
        self._stream_path = None
        self._closing = False

    # -- monitors ----------------------------------------------------------

    def list_monitors(self) -> list[Monitor]:
        """Every logical monitor, sorted by position, 1-based."""

        state = self._bus.call_sync(
            DISPLAY_CONFIG_NAME,
            DISPLAY_CONFIG_PATH,
            DISPLAY_CONFIG_NAME,
            "GetCurrentState",
            None,
            None,
            self._Gio.DBusCallFlags.NONE,
            5000,
            None,
        ).unpack()
        return parse_monitors(state)

    def connector_present(self, connector: str) -> bool:
        """Is ``connector`` still attached?

        This is what gives the "selected screen was disconnected" fallback a
        real trigger. Index-based targets alone cannot provide one: unplug the
        middle screen of three and index 2 still resolves -- to a different
        physical monitor.
        """

        if not connector:
            return True
        try:
            return any(monitor.name == connector for monitor in self.list_monitors()[1:])
        except Exception:  # noqa: BLE001 - cannot ask means do not claim it is gone
            return True

    def resolve_target(self, capture_target: str) -> Monitor | None:
        """Map a capture target onto a monitor, or None for the ``all`` box."""

        return resolve_from_monitors(self.list_monitors(), capture_target)

    # -- streaming ---------------------------------------------------------

    def open_stream(self, capture_target: str) -> int:
        """Start recording ``capture_target`` and return its PipeWire node id.

        ⚠️ **Always ``RecordArea``, never ``RecordMonitor`` -- including for a
        single screen.** Measured on this rig 2026-09-08: ``RecordMonitor`` on
        DP-2 (a Dell 32" at 3840x2160@240 with ``refresh-rate-mode: variable``)
        delivered **no frames at all**, while ``RecordArea`` over that monitor's
        identical rectangle delivered one in under 0.1 s. The two 60 Hz outputs
        worked either way. VRR is the only property that differs between the
        failing output and the working ones, but causation is UNCONFIRMED -- and
        it is intermittent: the same call succeeded earlier the same day, which
        fits VRR engaging on content.

        That intermittency is exactly why there is no per-monitor special case
        here. One path that always works beats a fast path that works until the
        user starts a game -- on the primary gaming monitor, which is the whole
        point of this program.

        The target is resolved FRESH on every call. It has to be: ``RecordArea``
        takes coordinates, not a connector, so a stale rectangle would not fail
        after a monitor is unplugged -- it would silently record whatever now
        occupies those coordinates, and the caller's fallback would never fire.
        """

        self.close()

        monitor = self.resolve_target(capture_target)
        whole_desktop = monitor is None
        if whole_desktop:
            monitor = self.list_monitors()[0]

        # Log a REPEAT of the same target at debug. While the compositor is
        # inhibited this method is retried on the sync interval, and an
        # unconditional INFO here prints the same rectangle several times a
        # second for the whole time the screen is asleep -- burying the one
        # line that matters. A genuine change of target still logs at INFO.
        target = (monitor.name, monitor.left, monitor.top, monitor.width, monitor.height)
        level = logging.DEBUG if target == self._last_logged_target else logging.INFO
        self._last_logged_target = target

        if whole_desktop:
            self._logger.log(
                level,
                "Recording the whole desktop: %dx%d at (%d, %d)",
                monitor.width,
                monitor.height,
                monitor.left,
                monitor.top,
            )
        else:
            self._logger.log(
                level,
                "Recording %s (index %d): %dx%d at (%d, %d)",
                monitor.name,
                monitor.index,
                monitor.width,
                monitor.height,
                monitor.left,
                monitor.top,
            )
        self.active_connector = monitor.name

        session_path = self._call(
            SCREENCAST_PATH, SCREENCAST_NAME, "CreateSession", self._GLib.Variant("(a{sv})", ({},))
        ).unpack()[0]
        self._session_path = session_path

        properties = {"cursor-mode": self._GLib.Variant("u", _CURSOR_MODE_HIDDEN)}
        stream_path = self._call(
            session_path,
            f"{SCREENCAST_NAME}.Session",
            "RecordArea",
            self._GLib.Variant(
                "(iiiia{sv})",
                (monitor.left, monitor.top, monitor.width, monitor.height, properties),
            ),
        ).unpack()[0]
        self._stream_path = stream_path

        node_id: dict[str, int | None] = {"id": None}
        arrived = threading.Event()

        def on_stream_added(_c, _s, _p, _i, _sig, params):
            node_id["id"] = int(params.unpack()[0])
            arrived.set()

        self._subscriptions.append(
            self._bus.signal_subscribe(
                None,
                f"{SCREENCAST_NAME}.Stream",
                "PipeWireStreamAdded",
                stream_path,
                None,
                self._Gio.DBusSignalFlags.NONE,
                on_stream_added,
            )
        )
        self._subscribe_invalidations(session_path, stream_path)

        self._call(session_path, f"{SCREENCAST_NAME}.Session", "Start")

        if not arrived.wait(10.0):
            raise MutterUnavailable("Mutter never reported a PipeWire node for the stream")
        assert node_id["id"] is not None
        return node_id["id"]

    def _subscribe_invalidations(self, session_path: str, stream_path: str) -> None:
        """Watch for the three ways a capture session stops being valid.

        Without this, unplugging a monitor leaves the pipeline dead and the
        lights frozen on the last frame indefinitely -- the same shape of bug as
        the latched off-state that ``enforce_lights_off`` was written to fix.
        """

        def invalidate(reason: str):
            def handler(*_args):
                if self._closing:
                    return
                self._logger.warning("Capture session invalidated (%s); rebuilding", reason)
                callback = self._on_invalidated
                if callback is not None:
                    callback(reason)

            return handler

        self._subscriptions.append(
            self._bus.signal_subscribe(
                None, f"{SCREENCAST_NAME}.Session", "Closed", session_path,
                None, self._Gio.DBusSignalFlags.NONE, invalidate("session closed"),
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                None, f"{SCREENCAST_NAME}.Stream", "Closed", stream_path,
                None, self._Gio.DBusSignalFlags.NONE, invalidate("stream closed"),
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                None, DISPLAY_CONFIG_NAME, "MonitorsChanged", DISPLAY_CONFIG_PATH,
                None, self._Gio.DBusSignalFlags.NONE, invalidate("monitor layout changed"),
            )
        )

    def _call(self, path: str, interface: str, method: str, params=None):
        """Every ScreenCast D-Bus call goes through here, including the retries.

        The translation below lives at this choke point on purpose: ``CreateSession``,
        ``RecordArea`` and ``Stop`` can all be refused for the same transient reason,
        so recognising it once here covers the class instead of the one method that
        happened to be observed failing.
        """

        try:
            return self._bus.call_sync(
                SCREENCAST_NAME,
                path,
                interface,
                method,
                params,
                None,
                self._Gio.DBusCallFlags.NONE,
                10000,
                None,
            )
        except self._GLib.Error as error:
            if is_transient_dbus_error(error):
                raise CaptureUnavailable(
                    f"{method} refused while capture is inhibited: {error.message}"
                ) from error
            raise


def parse_monitors(state: tuple) -> list[Monitor]:
    """Turn ``DisplayConfig.GetCurrentState`` into a positioned monitor list.

    Pure so the arithmetic can be tested against recorded output. Three things
    here are easy to get wrong and all three have a wrong answer that looks
    plausible:

    * **Scale.** A logical monitor's size is its mode divided by its scale. This
      rig's portrait panels run 3840x2160 at 1.25, so they are 3072x1728
      logical -- not 3840x2160.
    * **Rotation.** Transforms 1/3/5/7 are the quarter turns and swap the axes,
      so those panels end up 1728x3072. ``RecordArea`` was measured returning
      2160x3840 for that rectangle, i.e. the logical box rendered at the
      monitor's own scale -- which is why zones stay physically meaningful.
    * **Order.** Sorted by position, so index 2 keeps meaning the same screen
      across reboots. Mutter's own ordering is not stable enough to index into.
    """

    _serial, physical_monitors, logical_monitors, _props = state

    modes_by_connector: dict[str, tuple[int, int]] = {}
    for (connector, _vendor, _product, _serial_no), modes, _monitor_props in physical_monitors:
        for mode in modes:
            mode_props = mode[6] if len(mode) > 6 else {}
            if mode_props.get("is-current"):
                modes_by_connector[connector] = (int(mode[1]), int(mode[2]))
                break

    entries: list[tuple[int, int, Monitor]] = []
    for x_pos, y_pos, scale, transform, primary, monitor_specs, _lm_props in logical_monitors:
        if not monitor_specs:
            continue
        connector = monitor_specs[0][0]
        mode_width, mode_height = modes_by_connector.get(connector, (0, 0))
        width = round(mode_width / scale) if scale else mode_width
        height = round(mode_height / scale) if scale else mode_height
        if int(transform) in _ROTATED_TRANSFORMS:
            width, height = height, width
        entries.append(
            (
                int(x_pos),
                int(y_pos),
                Monitor(
                    index=0,
                    left=int(x_pos),
                    top=int(y_pos),
                    width=int(width),
                    height=int(height),
                    is_primary=bool(primary),
                    name=str(connector),
                ),
            )
        )

    entries.sort(key=lambda item: (item[0], item[1]))
    ordered = [
        Monitor(
            index=position,
            left=monitor.left,
            top=monitor.top,
            width=monitor.width,
            height=monitor.height,
            is_primary=monitor.is_primary,
            name=monitor.name,
        )
        for position, (_x, _y, monitor) in enumerate(entries, start=1)
    ]
    return [_bounding_box(ordered), *ordered]


def resolve_from_monitors(monitors: list[Monitor], capture_target: str) -> Monitor | None:
    """Map a capture target onto one of ``monitors``. ``None`` means "all"."""

    normalized = capture_target.strip().lower()
    if normalized == "all":
        return None
    if normalized == "primary":
        for monitor in monitors[1:]:
            if monitor.is_primary:
                return monitor
        return monitors[1] if len(monitors) > 1 else None
    index = int(normalized)
    for monitor in monitors[1:]:
        if monitor.index == index:
            return monitor
    raise ValueError(
        f"Monitor index {index} is not available ({len(monitors) - 1} screen(s) attached)"
    )


def _bounding_box(monitors: list[Monitor]) -> Monitor:
    """Index 0: the virtual desktop that encloses every screen."""

    if not monitors:
        return Monitor(index=0, left=0, top=0, width=0, height=0, name="all")
    left = min(monitor.left for monitor in monitors)
    top = min(monitor.top for monitor in monitors)
    right = max(monitor.left + monitor.width for monitor in monitors)
    bottom = max(monitor.top + monitor.height for monitor in monitors)
    return Monitor(index=0, left=left, top=top, width=right - left, height=bottom - top, name="all")
