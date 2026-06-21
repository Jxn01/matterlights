"""Detect when the display goes to sleep so the lights can follow it off.

Windows broadcasts ``GUID_CONSOLE_DISPLAY_STATE`` power-setting changes whenever
the monitor turns on, off, or dims. We listen for those by creating a hidden
message-only window on a daemon thread and registering for the notification.
Right after registration Windows delivers the current state, so the monitor
converges to the real value within a few milliseconds of starting.

If anything about that setup fails (or we are not on Windows) the factory returns
an always-on monitor, so the sync loop degrades to its normal behaviour and never
turns the lights off by mistake.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import sys
import threading
from typing import Protocol


LOGGER = logging.getLogger("matterlights.display_power")

# WM_POWERBROADCAST payload: GUID_CONSOLE_DISPLAY_STATE carries a DWORD whose
# value is 0 = off, 1 = on, 2 = dimmed. We treat dimmed as still-on.
_WM_POWERBROADCAST = 0x0218
_PBT_POWERSETTINGCHANGE = 0x8013
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000
_HWND_MESSAGE = -3
_DISPLAY_STATE_OFF = 0


class DisplayMonitor(Protocol):
    def is_display_on(self) -> bool: ...

    def stop(self) -> None: ...


class AlwaysOnDisplayMonitor:
    """Fallback used off-Windows or when notification setup fails."""

    def is_display_on(self) -> bool:
        return True

    def stop(self) -> None:
        return None


def start_display_monitor(logger: logging.Logger | None = None) -> DisplayMonitor:
    log = logger or LOGGER
    if sys.platform != "win32":
        return AlwaysOnDisplayMonitor()

    monitor = _Win32DisplayMonitor(log)
    if not monitor.start():
        log.warning("Display power monitoring unavailable; lights will not follow screen sleep.")
        return AlwaysOnDisplayMonitor()
    return monitor


if sys.platform == "win32":

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _POWERBROADCAST_SETTING(ctypes.Structure):
        _fields_ = [
            ("PowerSetting", _GUID),
            ("DataLength", wintypes.DWORD),
            ("Data", ctypes.c_ubyte * 4),
        ]

    _LRESULT = ctypes.c_ssize_t
    _LPARAM = ctypes.c_ssize_t
    _WPARAM = ctypes.c_size_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)

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

    _GUID_CONSOLE_DISPLAY_STATE = _GUID(
        0x6FE69556,
        0x704A,
        0x47A0,
        (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47),
    )

    def _guid_equal(left: _GUID, right: _GUID) -> bool:
        return (
            left.Data1 == right.Data1
            and left.Data2 == right.Data2
            and left.Data3 == right.Data3
            and bytes(left.Data4) == bytes(right.Data4)
        )

    class _Win32DisplayMonitor:
        def __init__(self, logger: logging.Logger) -> None:
            self._logger = logger
            self._on_event = threading.Event()
            self._on_event.set()  # Assume the display is on until told otherwise.
            self._ready = threading.Event()
            self._failed = False
            self._hwnd: int | None = None
            self._notification_handle: int | None = None
            self._class_atom: int | None = None
            self._hinstance: int | None = None
            self._class_name = f"MatterLightsDisplayMonitor_{id(self)}"
            self._thread = threading.Thread(target=self._run, name="display-power-monitor", daemon=True)
            # Keep strong references so the WNDPROC trampoline is not garbage collected.
            self._wndproc = _WNDPROC(self._window_proc)
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_signatures()

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
            user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            user32.RegisterPowerSettingNotification.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_GUID),
                wintypes.DWORD,
            ]
            user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL
            user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
            # GetMessageW is tri-state (-1 error, 0 WM_QUIT, >0 message), not a boolean.
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

        def start(self) -> bool:
            self._thread.start()
            self._ready.wait(timeout=2.0)
            return not self._failed

        def is_display_on(self) -> bool:
            return self._on_event.is_set()

        def stop(self) -> None:
            hwnd = self._hwnd
            if hwnd:
                try:
                    self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                except OSError:
                    pass

        def _run(self) -> None:
            try:
                self._create_window_and_register()
            except OSError:
                self._failed = True
                self._logger.exception("Failed to start display power monitoring")
                self._teardown()  # Release any window/class created before the failure.
                self._ready.set()
                return
            self._ready.set()
            self._pump_messages()

        def _create_window_and_register(self) -> None:
            self._hinstance = self._kernel32.GetModuleHandleW(None)

            window_class = _WNDCLASS()
            window_class.lpfnWndProc = self._wndproc
            window_class.hInstance = self._hinstance
            window_class.lpszClassName = self._class_name
            atom = self._user32.RegisterClassW(ctypes.byref(window_class))
            if not atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self._class_atom = atom

            hwnd = self._user32.CreateWindowExW(
                0,
                self._class_name,
                "MatterLights Display Monitor",
                0,
                0,
                0,
                0,
                0,
                _HWND_MESSAGE,
                None,
                self._hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd

            handle = self._user32.RegisterPowerSettingNotification(
                hwnd,
                ctypes.byref(_GUID_CONSOLE_DISPLAY_STATE),
                _DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._notification_handle = handle

        def _pump_messages(self) -> None:
            msg = wintypes.MSG()
            try:
                while True:
                    result = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if result == 0 or result == -1:
                        break
                    self._user32.TranslateMessage(ctypes.byref(msg))
                    self._user32.DispatchMessageW(ctypes.byref(msg))
            except OSError:
                self._logger.exception("Display power message loop stopped")
            finally:
                self._teardown()

        def _teardown(self) -> None:
            # Idempotent: runs once after the pump exits, and again on setup failure.
            # The window and class must be released on the thread that created them.
            if self._notification_handle:
                try:
                    self._user32.UnregisterPowerSettingNotification(self._notification_handle)
                except OSError:
                    pass
                self._notification_handle = None
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
            if message == _WM_POWERBROADCAST and wparam == _PBT_POWERSETTINGCHANGE:
                self._handle_power_setting(lparam)
                return 1
            if message == _WM_DESTROY:
                self._hwnd = None
                self._user32.PostQuitMessage(0)
                return 0
            if message == _WM_CLOSE:
                self._user32.DestroyWindow(hwnd)
                return 0
            return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

        def _handle_power_setting(self, lparam) -> None:
            try:
                setting = ctypes.cast(lparam, ctypes.POINTER(_POWERBROADCAST_SETTING)).contents
            except (ValueError, OSError):
                return
            if not _guid_equal(setting.PowerSetting, _GUID_CONSOLE_DISPLAY_STATE):
                return
            display_on = setting.Data[0] != _DISPLAY_STATE_OFF
            if display_on:
                self._on_event.set()
            else:
                self._on_event.clear()
            self._logger.info("Display %s", "on" if display_on else "off")
