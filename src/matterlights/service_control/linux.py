"""Service control through ``systemctl --user``.

Everything the dashboard can do maps onto a user unit, including the zone
designer: it runs as a transient unit via ``systemd-run`` rather than as a child
process of the dashboard. That is not tidiness. A plain child sits inside the
dashboard's own cgroup, so stopping or restarting the dashboard kills the zone
designer with it, and finding it again afterwards means scanning process lists.
A transient unit gives start, stop and status for free.

⚠️ **Where a process must still be identified, match ``/proc/*/cmdline`` tokens
exactly -- never ``pgrep -f``.** The searching shell's own command line contains
the pattern, so ``pgrep -f`` matches itself and reports a process that is not
there. That has cost real debugging time on this machine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess

from matterlights.service_control import DASHBOARD, SYNC, ZONE_UI, ServiceStatus

LOGGER = logging.getLogger("matterlights.service_control.linux")

UNIT_NAMES = {
    SYNC: "matterlights-sync.service",
    DASHBOARD: "matterlights-dashboard.service",
    ZONE_UI: "matterlights-zone-ui.service",
}

ZONE_UI_MODULE = "matterlights.zone_ui"

_SHOW_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
)


def parse_systemctl_show(output: str, unit: str) -> ServiceStatus:
    """Turn ``systemctl show`` key=value lines into a ServiceStatus.

    ``LoadState=not-found`` is how systemd says a unit does not exist; it is not
    an error and does not set a non-zero exit code, so it has to be read out of
    the output rather than inferred from the return code.

    Timestamps come back empty (``n/a`` in older systemd) when a unit has never
    run. Both are normalised to an empty string so the dashboard shows a blank
    rather than the word "n/a".
    """

    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    if values.get("LoadState") in (None, "not-found", "masked"):
        return ServiceStatus(exists=False, name=unit)

    def timestamp(key: str) -> str:
        raw = values.get(key, "")
        return "" if raw in ("", "n/a") else raw

    active = values.get("ActiveState", "")
    sub = values.get("SubState", "")
    return ServiceStatus(
        exists=True,
        name=unit,
        state=f"{active} ({sub})" if sub and sub != active else active,
        last_run=timestamp("ExecMainStartTimestamp"),
        next_run="",  # systemd services have no next-run; only timers do.
        last_result=values.get("Result", ""),
    )


class SystemdServiceControl:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or LOGGER

    # -- lifecycle ---------------------------------------------------------

    def start(self, service: str) -> None:
        if service == ZONE_UI and not self._unit_exists(UNIT_NAMES[ZONE_UI]):
            self._run_transient_zone_ui()
            return
        self._systemctl("start", UNIT_NAMES[service])

    def stop(self, service: str) -> int:
        unit = UNIT_NAMES[service]
        was_running = self.is_running(service)
        self._systemctl("stop", unit, check=False)
        return 1 if was_running else 0

    def restart(self, service: str) -> None:
        if service == ZONE_UI and not self._unit_exists(UNIT_NAMES[ZONE_UI]):
            self._systemctl("stop", UNIT_NAMES[ZONE_UI], check=False)
            self._run_transient_zone_ui()
            return
        self._systemctl("restart", UNIT_NAMES[service])

    def status(self, service: str) -> ServiceStatus:
        unit = UNIT_NAMES[service]
        result = self._systemctl(
            "show", unit, "-p", ",".join(_SHOW_PROPERTIES), check=False
        )
        return parse_systemctl_show(result.stdout, unit)

    def is_running(self, service: str) -> bool:
        result = self._systemctl("is-active", UNIT_NAMES[service], check=False)
        return result.stdout.strip() == "active"

    def restart_self(self, service: str) -> None:
        """Restart the dashboard from within the dashboard.

        ``systemd-run --on-active=1`` creates a TRANSIENT TIMER owned by the user
        manager, not a child of this process. That is the whole point: the
        restart stops this unit, and anything living in this unit's cgroup would
        be killed along with it before it could start anything back up. It is the
        same reasoning as the Windows side going through WMI rather than
        spawning a child.
        """

        unit = UNIT_NAMES[service]
        subprocess.Popen(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                "--on-active=1",
                "--unit=matterlights-restart-helper",
                "systemctl",
                "--user",
                "restart",
                unit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # -- helpers -----------------------------------------------------------

    def _run_transient_zone_ui(self) -> None:
        """Start the zone designer as a transient unit.

        Used when the packaged unit is not installed -- a manual checkout that
        never ran the installer. It still gets a real unit rather than an orphan
        child process, so stopping it later is ``systemctl stop`` and not a
        process hunt.
        """

        import sys

        subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                f"--unit={UNIT_NAMES[ZONE_UI].removesuffix('.service')}",
                f"--working-directory={_repo_root()}",
                sys.executable,
                "-m",
                ZONE_UI_MODULE,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def _unit_exists(self, unit: str) -> bool:
        result = self._systemctl("show", unit, "-p", "LoadState", check=False)
        return "LoadState=loaded" in result.stdout

    def _systemctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "systemctl failed"
            raise RuntimeError(message)
        return result


def zone_ui_process_ids() -> list[int]:
    """PIDs running ``-m matterlights.zone_ui``, by exact argv token match.

    Reads ``/proc/*/cmdline`` and compares NUL-separated arguments exactly.
    ``pgrep -f`` would be shorter and wrong: the pattern appears in the searching
    process's own command line, so it matches itself and reports a phantom.
    """

    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        tokens = [part.decode("utf-8", "replace") for part in argv if part]
        if "-m" in tokens and ZONE_UI_MODULE in tokens:
            pids.append(int(entry.name))
    return pids


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[3])
