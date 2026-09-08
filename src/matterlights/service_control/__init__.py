"""Start, stop and inspect the background services, whatever runs them.

The dashboard needs to answer "is the sync loop running?" and "restart it". On
Windows that is Task Scheduler driven through PowerShell; on Linux it is
``systemctl --user``. Both were embedded directly in ``dashboard.py``, which is
how a request handler ended up containing a here-doc of PowerShell -- so this
also takes 200-odd lines out of a 1700-line module.

Services are named LOGICALLY here (``sync``, ``dashboard``, ``zone-ui``) and each
backend maps them to its own naming. That mapping is the whole reason this
abstraction earns its place: a Windows scheduled task called
``MatterLights Screen Sync`` and a unit called ``matterlights-sync.service``
are the same idea with nothing textual in common.

``ServiceStatus`` deliberately keeps the JSON field names the dashboard's
JavaScript already renders (``taskName``, ``lastRunTime``, ``nextRunTime``,
``lastTaskResult``), so the front end needed no changes. The words are
Task-Scheduler-flavoured because that is where they came from; renaming them
would mean touching the UI for no behavioural gain.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sys
from typing import Any, Protocol

LOGGER = logging.getLogger("matterlights.service_control")

SYNC = "sync"
DASHBOARD = "dashboard"
ZONE_UI = "zone-ui"


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    exists: bool
    name: str
    state: str = ""
    last_run: str = ""
    next_run: str = ""
    last_result: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "taskName": self.name,
            "state": self.state,
            "lastRunTime": self.last_run,
            "nextRunTime": self.next_run,
            "lastTaskResult": self.last_result,
        }


class ServiceControl(Protocol):
    def start(self, service: str) -> None: ...

    def stop(self, service: str) -> int: ...

    def restart(self, service: str) -> None: ...

    def status(self, service: str) -> ServiceStatus: ...

    def is_running(self, service: str) -> bool: ...

    def restart_self(self, service: str) -> None:
        """Restart a service from inside that same service.

        Needs a helper that outlives the caller and lives outside its process
        group, or stopping the service kills the thing meant to start it again.
        """


def get_service_control(logger: logging.Logger | None = None) -> ServiceControl:
    log = logger or LOGGER
    if sys.platform == "win32":
        from matterlights.service_control.windows import WindowsServiceControl

        return WindowsServiceControl(log)

    from matterlights.service_control.linux import SystemdServiceControl

    return SystemdServiceControl(log)
