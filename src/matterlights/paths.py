"""Where MatterLights keeps files it writes itself.

Not configuration -- configuration lives in ``.env`` and the user owns it. This
is the machine state the program generates: the log, and the portal restore
token. Those belong in the platform's state directory, and the two platforms
disagree about where that is.

The previous fallback for "not Windows" was the current working directory, which
meant a log file appearing in whatever directory the process happened to start
in -- usually the repo, where it is gitignored and therefore invisible until it
is large.

⚠️ **NOTHING HERE MAY RAISE.** ``load_settings()`` computes the default log path
as an *argument expression*, so Python evaluates it on every call even when
``LOG_PATH`` is set explicitly. An exception raised here is therefore not a
misplaced log file -- it is a startup crash for every entry point, including
``lights_off`` under the SYSTEM shutdown task, whose whole purpose is to run
while the machine is going down. Degrade to a worse location instead.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

_APP_DIR_NAME = "matterlights"


def _home_or_none() -> Path | None:
    """``Path.home()`` that reports failure instead of raising.

    ⚠️ **ASYMMETRIC BY PLATFORM, WHICH IS WHY THIS IS EASY TO MISS FROM LINUX.**
    ``Path.home()`` is ``Path("~").expanduser()``. On POSIX that falls back to
    the passwd database when ``HOME`` is unset, so it effectively cannot fail.
    On Windows it is purely environmental -- ``USERPROFILE``, or
    ``HOMEDRIVE`` + ``HOMEPATH`` -- and with none of them set ``ntpath``
    returns ``"~"`` unexpanded, which pathlib turns into
    ``RuntimeError("Could not determine home directory.")``.

    So a home lookup that is provably safe on Linux is a live exception path on
    Windows, in exactly the stripped-environment contexts (service and
    scheduled-task launches) where this program is expected to keep working.
    """

    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def expand_user(path: Path) -> Path:
    """``Path.expanduser()`` that leaves the path alone when home is unknown.

    Same asymmetry as :func:`_home_or_none`, reached through user input rather
    than through our own default: a ``~`` in ``LOG_PATH``, ``LIGHT_ZONE_FILE``
    or ``MATTERLIGHTS_ENV_FILE`` is expanded during ``load_settings()``, so on
    Windows a stripped environment turns a tilde in a config file into a crash
    at startup.

    Returning the unexpanded path is the better failure: a literal ``~``
    directory is visible and recoverable, a process that will not start is not.
    """

    try:
        return path.expanduser()
    except (RuntimeError, OSError):
        return path


def state_dir() -> Path:
    r"""The directory for state this program writes.

    Windows: ``%LOCALAPPDATA%\matterlights``.
    Linux and everything else: ``$XDG_STATE_HOME/matterlights``, defaulting to
    ``~/.local/state/matterlights`` per the XDG Base Directory spec -- state,
    not cache and not config: it should survive a reboot but nobody edits it.

    Falls back to the temp directory only when the home directory cannot be
    determined at all. That is a genuinely degenerate environment, and a log in
    ``%TEMP%`` beats refusing to start; see the module docstring.
    """

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _APP_DIR_NAME

    xdg_state_home = os.getenv("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / _APP_DIR_NAME

    home = _home_or_none()
    if home is not None:
        return home / ".local" / "state" / _APP_DIR_NAME

    return Path(tempfile.gettempdir()) / _APP_DIR_NAME


def default_log_path() -> Path:
    return state_dir() / "matterlights.log"


def portal_token_path() -> Path:
    """Where the xdg-desktop-portal restore token is kept.

    Deliberately here rather than in ``.env``: it is a machine-specific handle
    the portal issued to this install, not something a user sets or copies
    between machines. Putting it in ``.env`` would invite exactly that.
    """

    return state_dir() / "portal-restore-token"
