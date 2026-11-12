"""Whether the sync loop has driven the lights since boot.

A shutdown must not darken the room when nothing was syncing: the machine can be powered on and off with nobody at it. So the loop leaves a
mark when it starts, and the shutdown layer on Linux only acts if it finds one. The mark lives in a folder of the SYSTEM unit, because
/run/user/1000 is torn down with the session, before that unit stops. /run empties at every boot, which is what makes it mean "this boot".

Windows keeps its old behaviour: it has no such use, and no such folder.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER = logging.getLogger("matterlights.sync_mark")
SYNC_MARK = Path("/run/matterlights/synced")


def leave_mark() -> None:
    if sys.platform != "linux":
        return
    try:
        SYNC_MARK.touch()
    except OSError as error:
        LOGGER.warning("Could not leave the sync mark: %s", error)


def sync_ran() -> bool:
    return sys.platform != "linux" or SYNC_MARK.exists()
