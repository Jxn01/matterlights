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

import logging
import threading
from typing import Callable

from matterlights.capture import Monitor

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
        ensure_main_loop()

    # -- lifecycle ---------------------------------------------------------

    def set_invalidation_callback(self, callback: Callable[[str], None]) -> None:
        """Called with a reason when the session, stream or layout goes away."""

        self._on_invalidated = callback

    def close(self) -> None:
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

    # -- monitors ----------------------------------------------------------

    def list_monitors(self) -> list[Monitor]:
        """Every logical monitor, sorted by position, 1-based.

        Sorted by ``(x, y)`` rather than by whatever order Mutter returns, so a
        given screen keeps the same index across reboots -- otherwise
        ``SCREEN_CAPTURE_TARGET=2`` would silently mean a different screen after
        a replug.
        """

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

        _serial, physical_monitors, logical_monitors, _props = state

        modes_by_connector: dict[str, tuple[int, int]] = {}
        for (connector, _vendor, _product, _serial_no), modes, _props in physical_monitors:
            for mode in modes:
                mode_props = mode[6] if len(mode) > 6 else {}
                if mode_props.get("is-current"):
                    modes_by_connector[connector] = (int(mode[1]), int(mode[2]))
                    break

        entries: list[tuple[int, int, Monitor]] = []
        for x_pos, y_pos, scale, transform, primary, monitor_specs, _props in logical_monitors:
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
                        index=0,  # replaced below, once sorted
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

    def resolve_target(self, capture_target: str) -> Monitor | None:
        """Map a capture target onto a monitor, or None for the ``all`` box."""

        monitors = self.list_monitors()
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

    # -- streaming ---------------------------------------------------------

    def open_stream(self, capture_target: str) -> int:
        """Start recording ``capture_target`` and return its PipeWire node id."""

        self.close()

        monitor = self.resolve_target(capture_target)
        session_path = self._call(
            SCREENCAST_PATH, SCREENCAST_NAME, "CreateSession", self._GLib.Variant("(a{sv})", ({},))
        ).unpack()[0]
        self._session_path = session_path

        properties = {"cursor-mode": self._GLib.Variant("u", _CURSOR_MODE_HIDDEN)}
        if monitor is None:
            box = self.list_monitors()[0]
            self._logger.info(
                "Recording the whole desktop: %dx%d at (%d, %d)",
                box.width,
                box.height,
                box.left,
                box.top,
            )
            stream_path = self._call(
                session_path,
                f"{SCREENCAST_NAME}.Session",
                "RecordArea",
                self._GLib.Variant(
                    "(iiiia{sv})", (box.left, box.top, box.width, box.height, properties)
                ),
            ).unpack()[0]
        else:
            self._logger.info("Recording monitor %s (index %d)", monitor.name, monitor.index)
            stream_path = self._call(
                session_path,
                f"{SCREENCAST_NAME}.Session",
                "RecordMonitor",
                self._GLib.Variant("(sa{sv})", (monitor.name, properties)),
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


def _bounding_box(monitors: list[Monitor]) -> Monitor:
    """Index 0: the virtual desktop that encloses every screen."""

    if not monitors:
        return Monitor(index=0, left=0, top=0, width=0, height=0, name="all")
    left = min(monitor.left for monitor in monitors)
    top = min(monitor.top for monitor in monitors)
    right = max(monitor.left + monitor.width for monitor in monitors)
    bottom = max(monitor.top + monitor.height for monitor in monitors)
    return Monitor(index=0, left=left, top=top, width=right - left, height=bottom - top, name="all")
