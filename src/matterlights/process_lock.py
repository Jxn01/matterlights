"""Single-instance guard for the sync loop.

Two sync loops driving the same Home Assistant lights fight each other — in custom
pattern mode they run on independent clocks and send overlapping transition
commands, which can lock Matter bulbs up until they are power-cycled. The
scheduled task only guards against the *task* starting twice; a manual launch
(``start-sync.ps1`` or ``python -m matterlights``) slips past it.

A session-local named mutex closes that gap: whichever loop starts first owns the
mutex, and any later loop sees it already exists and bows out. Windows releases
the mutex automatically when the owning process dies, so a crash never leaves a
stale lock behind.
"""

from __future__ import annotations

import logging
import sys


LOGGER = logging.getLogger("matterlights.process_lock")

# Session-local (not "Global\\") so it scopes to the interactive user session the
# task and any manual launch share.
_MUTEX_NAME = "MatterLightsScreenSyncSingleton"
_ERROR_ALREADY_EXISTS = 183


class _NullLock:
    def release(self) -> None:
        return None


def acquire_sync_singleton(logger: logging.Logger | None = None, name: str = _MUTEX_NAME) -> object | None:
    """Return a lock handle, or ``None`` if another sync loop already holds it.

    The returned object must be kept alive for the lifetime of the process. On any
    platform or error where the guard cannot be established, a no-op lock is
    returned so the loop still runs (failing open, never blocking a legitimate
    single instance). ``name`` is overridable so tests can use an isolated mutex.
    """

    log = logger or LOGGER
    if sys.platform != "win32":
        return _NullLock()

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateMutexW(None, True, name)
        last_error = ctypes.get_last_error()
        if not handle:
            log.warning("Could not create single-instance lock (error %s); continuing.", last_error)
            return _NullLock()
        if last_error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return _Win32Mutex(kernel32, handle)
    except OSError:
        log.warning("Single-instance lock unavailable; continuing without it.", exc_info=True)
        return _NullLock()


class _Win32Mutex:
    def __init__(self, kernel32, handle) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    def release(self) -> None:
        if self._handle:
            try:
                self._kernel32.ReleaseMutex(self._handle)
                self._kernel32.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None
