"""Turn every configured light off, once, then exit.

This exists because the in-process shutdown hook cannot be relied on. Measured
over eight days covering six shutdowns and two restarts, its callback never ran
once -- while the window it listens on was demonstrably alive and answering
messages from another process.

A system-level trigger runs this module instead, as a fresh process started
once the shutdown is already under way, so it does not depend on the sync loop
having survived:

* **Windows** -- a Task Scheduler task on System event **1074** (shutdown
  initiated), running as SYSTEM.
* **Linux** -- ``ExecStop=`` in ``matterlights-lights-off.service``, a SYSTEM
  unit ordered after ``network-online.target`` so that, since systemd stops
  units in reverse order, it runs while the network is still up.

⚠️ **It must run at SYSTEM level on both platforms, and for the same reason.**
On Windows, registered as an ordinary interactive task, it triggers on time --
one second after event 1074 -- and then dies with ``0xC000026B``
(``STATUS_DLL_INIT_FAILED_LOGOFF``): "the application failed to initialize
because the window station is shutting down". The interactive session is already
being destroyed by then. SYSTEM runs in session 0, which is still alive.

On Linux a *user* unit dies with the session for exactly the same reason, so the
layer that survives a shutdown has to live in the system manager. In both cases:
the thing being torn down cannot be the thing that reports the teardown.

Nothing here needs the user session -- the token comes from ``.env`` (located by
``MATTERLIGHTS_ENV_FILE``) and the request is plain HTTP with a bearer token.

It deliberately does the least work possible: read settings, send one grouped
turn_off, exit. No screen capture, no ``mss``/DXGI import, no singleton lock --
every one of those costs import time that the shutdown window does not have.
"""

from __future__ import annotations

import logging

from matterlights.config import load_settings
from matterlights.home_assistant import HomeAssistantClient
from matterlights.logging_setup import configure_logging


LOGGER = logging.getLogger("matterlights.lights_off")


def main() -> int:
    settings = load_settings()
    # Not rotating: the sync loop may still be alive and holding the same file.
    configure_logging(settings.log_path, rotating=False)

    if not settings.light_entities:
        LOGGER.warning("No lights configured; nothing to turn off")
        return 0

    client = HomeAssistantClient(
        settings.ha_url,
        settings.ha_token,
        timeout_seconds=settings.session_end_timeout_seconds,
    )

    LOGGER.info("Session ending; turning lights off")
    accepted = client.turn_off_lights_urgently(
        list(settings.light_entities),
        settings.session_end_timeout_seconds,
    )
    if accepted:
        LOGGER.info("Session ending; lights off")
    else:
        # Exit 0 regardless: a task that reports failure whenever Home Assistant
        # is slow would cry wolf at every shutdown. The warning is the signal.
        # Do not read this as "sent, so it worked" -- a timeout cancels the rest
        # of the service call, and has been observed leaving bulbs on.
        LOGGER.warning("Session ending; turn-off did not confirm before the timeout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
