"""Turn the lights off when Windows shuts down, restarts, or logs off.

A background process is simply killed at shutdown, so the lights would otherwise
stay on at whatever colour they last had. Windows does notify running
applications first, and this module listens on the two independent paths that
notification arrives by:

* ``WM_ENDSESSION`` delivered to a top-level window -- the documented signal that
  the session really is ending (as opposed to ``WM_QUERYENDSESSION``, which can
  still be cancelled), and
* the console ``CTRL_SHUTDOWN``/``CTRL_LOGOFF``/``CTRL_CLOSE`` events, which the
  sync loop receives because it runs in a (hidden) console.

Either path invokes the callback exactly once.

Note this deliberately creates a normal hidden top-level window rather than the
message-only window :mod:`matterlights.display_power` uses: message-only windows
are not enumerated as top-level windows and therefore never receive session-end
messages. ``WS_EX_TOOLWINDOW`` plus never calling ``ShowWindow`` keeps it out of
the taskbar and Alt+Tab.

Windows allows roughly five seconds to clean up after ``WM_ENDSESSION``, so the
callback must be quick. Turning every light off is a single grouped Home
Assistant request, which fits comfortably.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import sys
import threading
from typing import Callable, Protocol


LOGGER = logging.getLogger("matterlights.shutdown_hook")

_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WS_EX_TOOLWINDOW = 0x00000080

_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_SESSION_END_CTRL_EVENTS = frozenset({_CTRL_CLOSE_EVENT, _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT})


class ShutdownHook(Protocol):
    def stop(self) -> None: ...


class _NullHook:
    def stop(self) -> None:
        return None


def start_shutdown_hook(
    on_session_end: Callable[[], None],
    logger: logging.Logger | None = None,
) -> ShutdownHook:
    """Call ``on_session_end`` once when the Windows session is ending.

    Falls back to a no-op hook off Windows or if the window cannot be created,
    so the sync loop always keeps running.
    """

    log = logger or LOGGER
    if sys.platform != "win32":
        return _NullHook()

    hook = _Win32ShutdownHook(on_session_end, log)
    if not hook.start():
        log.warning("Shutdown detection unavailable; lights will not turn off at shutdown.")
        return _NullHook()
    return hook


if sys.platform == "win32":

    _LRESULT = ctypes.c_ssize_t
    _LPARAM = ctypes.c_ssize_t
    _WPARAM = ctypes.c_size_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)
    _HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    class _WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _Win32ShutdownHook:
        def __init__(self, on_session_end: Callable[[], None], logger: logging.Logger) -> None:
            self._on_session_end = on_session_end
            self._logger = logger
            self._fired = threading.Lock()
            self._has_fired = False
            self._ready = threading.Event()
            self._failed = False
            self._hwnd: int | None = None
            self._class_atom: int | None = None
            self._hinstance: int | None = None
            self._class_name = f"MatterLightsShutdownHook_{id(self)}"
            # Keep strong references so the ctypes trampolines are not collected.
            self._wndproc = _WNDPROC(self._window_proc)
            self._ctrl_handler = _HANDLER_ROUTINE(self._console_ctrl_handler)
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._thread = threading.Thread(target=self._run, name="shutdown-hook", daemon=True)
            self._configure_signatures()

        @property
        def hwnd(self) -> int | None:
            return self._hwnd

        def _configure_signatures(self) -> None:
            user32 = self._user32
            kernel32 = self._kernel32
            user32.DefWindowProcW.restype = _LRESULT
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            ]
            user32.GetMessageW.restype = ctypes.c_int
            user32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DispatchMessageW.restype = _LRESULT
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.UnregisterClassW.restype = wintypes.BOOL
            user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            kernel32.SetConsoleCtrlHandler.argtypes = [_HANDLER_ROUTINE, wintypes.BOOL]

        def start(self) -> bool:
            self._thread.start()
            self._ready.wait(timeout=2.0)
            if self._failed:
                return False
            # Independent second path; harmless if the process has no console.
            try:
                self._kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True)
            except OSError:
                self._logger.debug("Console control handler unavailable", exc_info=True)
            return True

        def stop(self) -> None:
            hwnd = self._hwnd
            if hwnd:
                try:
                    self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                except OSError:
                    pass

        def fire(self, reason: str) -> None:
            """Run the callback at most once, whichever path detects the end."""

            with self._fired:
                if self._has_fired:
                    return
                self._has_fired = True
            try:
                self._logger.info("Session ending (%s); turning lights off", reason)
                self._on_session_end()
            except Exception:
                self._logger.exception("Failed to turn lights off during shutdown")

        def _run(self) -> None:
            try:
                self._create_window()
            except OSError:
                self._failed = True
                self._logger.exception("Failed to start shutdown detection")
                self._teardown()
                self._ready.set()
                return
            self._ready.set()
            self._pump_messages()

        def _create_window(self) -> None:
            self._hinstance = self._kernel32.GetModuleHandleW(None)
            window_class = _WNDCLASS()
            window_class.lpfnWndProc = self._wndproc
            window_class.hInstance = self._hinstance
            window_class.lpszClassName = self._class_name
            atom = self._user32.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self._class_atom = atom

            # Top-level (parent NULL) so it receives session-end messages, but never
            # shown and flagged as a tool window so it stays out of the UI.
            hwnd = self._user32.CreateWindowExW(
                _WS_EX_TOOLWINDOW,
                self._class_name,
                "MatterLights Shutdown Hook",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                self._hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd

        def _pump_messages(self) -> None:
            msg = wintypes.MSG()
            try:
                while True:
                    result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if result in (0, -1):
                        break
                    self._user32.TranslateMessage(ctypes.byref(msg))
                    self._user32.DispatchMessageW(ctypes.byref(msg))
            except OSError:
                self._logger.exception("Shutdown hook message loop stopped")
            finally:
                self._teardown()

        def _teardown(self) -> None:
            if self._hwnd:
                try:
                    self._user32.DestroyWindow(self._hwnd)
                except OSError:
                    pass
                self._hwnd = None
            if self._class_atom:
                try:
                    self._user32.UnregisterClassW(self._class_name, self._hinstance)
                except OSError:
                    pass
                self._class_atom = None

        def _window_proc(self, hwnd, message, wparam, lparam):
            if message == _WM_QUERYENDSESSION:
                # Never block shutdown; wait for WM_ENDSESSION to act, since a
                # query can still be cancelled by another application.
                return 1
            if message == _WM_ENDSESSION:
                if wparam:
                    self.fire("WM_ENDSESSION")
                return 0
            if message == _WM_DESTROY:
                self._hwnd = None
                self._user32.PostQuitMessage(0)
                return 0
            if message == _WM_CLOSE:
                self._user32.DestroyWindow(hwnd)
                return 0
            return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

        def _console_ctrl_handler(self, event: int) -> bool:
            if event in _SESSION_END_CTRL_EVENTS:
                self.fire(f"console event {event}")
            return False  # Let default handling continue.
