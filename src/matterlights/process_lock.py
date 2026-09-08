"""Single-instance guard for the sync loop.

Two sync loops driving the same Home Assistant lights fight each other — in custom
pattern mode they run on independent clocks and send overlapping transition
commands, which can lock Matter bulbs up until they are power-cycled. The
scheduled task only guards against the *task* starting twice; a manual launch
(``start-sync.ps1`` or ``python -m matterlights``) slips past it, and on Linux
``systemctl --user start`` and a manual run are two different things entirely.

A session-local named mutex on Windows, and an ``flock`` on Linux, close that
gap: whichever loop starts first owns it, and any later loop finds it held and
bows out.

Both primitives are chosen for the same property -- the KERNEL releases them when
the owning process dies, however it dies -- so a crash never leaves a stale lock
that blocks every future start.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import tempfile


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
        return _acquire_flock(log, name)

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


def _acquire_flock(log: logging.Logger, name: str) -> object | None:
    """POSIX equivalent of the named mutex: an exclusive flock.

    ``flock`` is the right primitive rather than a pidfile because the KERNEL
    releases it when the holding process dies, however it dies. A pidfile
    written by a process that then segfaults is a stale lock that blocks every
    future start until someone notices and deletes it -- and this program is
    meant to run unattended.

    The file lives in ``$XDG_RUNTIME_DIR``, which is a tmpfs cleared at logout,
    so the lock is scoped to one login session exactly as the Windows mutex is
    scoped to one interactive session.
    """

    import fcntl

    lock_path = _lock_path(name)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w", encoding="utf-8")
    except OSError:
        log.warning("Single-instance lock unavailable; continuing without it.", exc_info=True)
        return _NullLock()

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None

    try:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        pass
    return _PosixFlock(handle)


def _lock_path(name: str) -> "Path":
    runtime_dir = os.getenv("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path(tempfile.gettempdir())
    return base / f"{name}.lock"


class _PosixFlock:
    def __init__(self, handle) -> None:
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None


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
