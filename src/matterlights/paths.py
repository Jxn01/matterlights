"""Where MatterLights keeps files it writes itself.

Not configuration -- configuration lives in ``.env`` and the user owns it. This
is the machine state the program generates: the log, and the portal restore
token. Those belong in the platform's state directory, and the two platforms
disagree about where that is.

The previous fallback for "not Windows" was the current working directory, which
meant a log file appearing in whatever directory the process happened to start
in -- usually the repo, where it is gitignored and therefore invisible until it
is large.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_DIR_NAME = "matterlights"


def state_dir() -> Path:
    """The directory for state this program writes.

    Windows: ``%LOCALAPPDATA%\\matterlights``.
    Linux and everything else: ``$XDG_STATE_HOME/matterlights``, defaulting to
    ``~/.local/state/matterlights`` per the XDG Base Directory spec -- state,
    not cache and not config: it should survive a reboot but nobody edits it.
    """

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _APP_DIR_NAME

    xdg_state_home = os.getenv("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / _APP_DIR_NAME

    return Path.home() / ".local" / "state" / _APP_DIR_NAME


def default_log_path() -> Path:
    return state_dir() / "matterlights.log"


def portal_token_path() -> Path:
    """Where the xdg-desktop-portal restore token is kept.

    Deliberately here rather than in ``.env``: it is a machine-specific handle
    the portal issued to this install, not something a user sets or copies
    between machines. Putting it in ``.env`` would invite exactly that.
    """

    return state_dir() / "portal-restore-token"
