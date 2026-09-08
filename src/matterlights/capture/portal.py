"""Acquire a PipeWire node through xdg-desktop-portal.

This is the standard, cross-desktop route, and the fallback for anything that is
not GNOME. It is a fallback rather than the default because it raises a
**consent dialog**: the first run asks the user to pick a screen and approve
sharing. ``persist_mode=2`` asks the portal for a ``restore_token`` so later runs
skip the dialog, and that token is kept in the XDG state directory.

⚠️ **A consequence callers must surface rather than hide:** on this path the
*user* chooses the screen in the dialog, so ``SCREEN_CAPTURE_TARGET`` and the
dashboard's Capture Screen picker cannot select it. The dashboard says so instead
of offering a control that silently does nothing.

THE REQUEST/RESPONSE DANCE. Every portal method returns immediately with a
Request object path and delivers its real answer later as a ``Response`` signal
on that path. The subscription therefore has to exist *before* the call is made,
which means computing the path rather than reading it from the reply -- hence
``_request_path``. Subscribing after the call is a race that works on a fast
machine and hangs on a slow one.
"""

from __future__ import annotations

import logging
import secrets
import threading

from matterlights import paths
from matterlights.capture import Monitor
from matterlights.capture.mutter import MutterUnavailable, _gi, ensure_main_loop

LOGGER = logging.getLogger("matterlights.capture.portal")

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

_SOURCE_TYPE_MONITOR = 1
_CURSOR_MODE_HIDDEN = 1
# 2 = persist until explicitly revoked. Without it every start prompts again.
_PERSIST_MODE_PERMANENT = 2

_RESPONSE_SUCCESS = 0
_RESPONSE_CANCELLED = 1


class PortalUnavailable(MutterUnavailable):
    """The portal is missing, refused, or the user declined."""


def portal_available() -> bool:
    try:
        Gio, GLib = _gi()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            PORTAL_NAME,
            PORTAL_PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (SCREENCAST_IFACE, "version")),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return True
    except Exception:  # noqa: BLE001
        LOGGER.debug("xdg-desktop-portal ScreenCast not available", exc_info=True)
        return False


class PortalSource:
    """Portal equivalent of :class:`~matterlights.capture.mutter.MutterSource`.

    Presents the same interface so the backend does not care which one it got,
    with one honest difference: :attr:`target_is_user_selected` is True, because
    the screen came from a dialog rather than from configuration.
    """

    target_is_user_selected = True

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER
        Gio, GLib = _gi()
        self._Gio = Gio
        self._GLib = GLib
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._session_handle: str | None = None
        self._on_invalidated = None
        self._subscriptions: list[int] = []
        self.active_connector = ""
        self._streams: list[tuple[int, dict]] = []
        ensure_main_loop()

    # -- interface parity with MutterSource --------------------------------

    def set_invalidation_callback(self, callback) -> None:
        self._on_invalidated = callback

    def connector_present(self, connector: str) -> bool:
        # The portal never tells us which connector it granted, so there is
        # nothing to check. Claiming a screen is gone on no evidence would send
        # the backend into a pointless fallback loop.
        return True

    def list_monitors(self) -> list[Monitor]:
        """What the portal granted, which is all it will say.

        The portal deliberately does not enumerate screens -- that is the point
        of it, the app only learns about what the user chose to share.
        """

        monitors: list[Monitor] = []
        for index, (_node_id, properties) in enumerate(self._streams, start=1):
            size = properties.get("size", (0, 0))
            position = properties.get("position", (0, 0))
            monitors.append(
                Monitor(
                    index=index,
                    left=int(position[0]),
                    top=int(position[1]),
                    width=int(size[0]),
                    height=int(size[1]),
                    is_primary=index == 1,
                    name=f"portal-{index}",
                )
            )
        box = Monitor(
            index=0,
            left=0,
            top=0,
            width=max((m.left + m.width for m in monitors), default=0),
            height=max((m.top + m.height for m in monitors), default=0),
            name="all",
        )
        return [box, *monitors]

    def resolve_target(self, capture_target: str) -> Monitor | None:
        monitors = self.list_monitors()
        return monitors[1] if len(monitors) > 1 else None

    def close(self) -> None:
        for subscription in self._subscriptions:
            try:
                self._bus.signal_unsubscribe(subscription)
            except Exception:  # noqa: BLE001
                pass
        self._subscriptions.clear()
        if self._session_handle:
            try:
                self._bus.call_sync(
                    PORTAL_NAME,
                    self._session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    self._Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
            except Exception:  # noqa: BLE001
                pass
        self._session_handle = None
        self._streams = []

    # -- streaming ---------------------------------------------------------

    def open_stream(self, capture_target: str) -> int:
        """Run the full portal handshake and return a PipeWire node id.

        ``capture_target`` is accepted for interface parity and cannot be
        honoured: the portal's whole design is that the user picks the source.
        """

        self.close()

        session_token = _token()
        response = self._request(
            "CreateSession",
            self._GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": self._GLib.Variant("s", _token()),
                        "session_handle_token": self._GLib.Variant("s", session_token),
                    },
                ),
            ),
        )
        self._session_handle = response.get("session_handle")
        if not self._session_handle:
            raise PortalUnavailable("Portal did not return a session handle")

        options = {
            "handle_token": self._GLib.Variant("s", _token()),
            "types": self._GLib.Variant("u", _SOURCE_TYPE_MONITOR),
            "multiple": self._GLib.Variant("b", False),
            "cursor_mode": self._GLib.Variant("u", _CURSOR_MODE_HIDDEN),
            "persist_mode": self._GLib.Variant("u", _PERSIST_MODE_PERMANENT),
        }
        saved_token = _read_restore_token()
        if saved_token:
            options["restore_token"] = self._GLib.Variant("s", saved_token)
            self._logger.info("Reusing the saved portal permission; no dialog expected")
        else:
            self._logger.warning(
                "No saved portal permission: a screen-sharing dialog will appear. "
                "Approve it once and the choice is remembered."
            )

        self._request(
            "SelectSources",
            self._GLib.Variant("(oa{sv})", (self._session_handle, options)),
        )

        started = self._request(
            "Start",
            self._GLib.Variant(
                "(osa{sv})",
                (self._session_handle, "", {"handle_token": self._GLib.Variant("s", _token())}),
            ),
        )

        restore_token = started.get("restore_token")
        if restore_token:
            _write_restore_token(str(restore_token))

        streams = started.get("streams") or []
        if not streams:
            raise PortalUnavailable("Portal granted no streams")
        self._streams = [(int(node), dict(props)) for node, props in streams]
        self.active_connector = "portal"
        return self._streams[0][0]

    def _request(self, method: str, params) -> dict:
        """Call a portal method and wait for its Response signal."""

        handle_token = params.unpack()[-1].get("handle_token")
        request_path = _request_path(self._bus.get_unique_name(), handle_token)

        result: dict = {}
        answered = threading.Event()

        def on_response(_c, _s, _p, _i, _sig, signal_params):
            code, payload = signal_params.unpack()
            result["code"] = code
            result.update(payload)
            answered.set()

        subscription = self._bus.signal_subscribe(
            PORTAL_NAME,
            REQUEST_IFACE,
            "Response",
            request_path,
            None,
            self._Gio.DBusSignalFlags.NONE,
            on_response,
        )
        try:
            self._bus.call_sync(
                PORTAL_NAME,
                PORTAL_PATH,
                SCREENCAST_IFACE,
                method,
                params,
                None,
                self._Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            # Generous: SelectSources and Start can sit on a human clicking.
            if not answered.wait(300.0):
                raise PortalUnavailable(f"Portal never answered {method}")
        finally:
            try:
                self._bus.signal_unsubscribe(subscription)
            except Exception:  # noqa: BLE001
                pass

        code = result.get("code")
        if code == _RESPONSE_CANCELLED:
            raise PortalUnavailable(f"The screen-sharing request was cancelled ({method})")
        if code != _RESPONSE_SUCCESS:
            raise PortalUnavailable(f"Portal {method} failed with response code {code}")
        return result


def _token() -> str:
    return f"matterlights{secrets.token_hex(8)}"


def _request_path(unique_name: str, handle_token: str) -> str:
    """The Request path a portal call WILL use, derived before making it.

    The sender part is the caller's unique bus name with the leading colon
    dropped and dots turned into underscores -- that is the documented mangling.
    Deriving it is what allows subscribing before the call and closing the race.
    """

    sender = unique_name.lstrip(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{handle_token}"


def _read_restore_token() -> str:
    path = paths.portal_token_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_restore_token(token: str) -> None:
    path = paths.portal_token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        LOGGER.warning("Could not save the portal permission to %s", path, exc_info=True)
