"""Restarting a Windows service must wait for it to actually stop.

⚠️ **A CROSS-PLATFORM TRAP THAT ONLY BITES ON WINDOWS.** ``restart`` means the
same thing on both platforms, but the primitives do not:

* ``systemctl --user restart`` is SYNCHRONOUS -- systemd stops the unit, waits
  for the process to exit, then starts it.
* ``Stop-ScheduledTask`` is ASYNCHRONOUS -- it signals termination and returns
  immediately, while the process is still alive.

So ``Stop-ScheduledTask ; Start-ScheduledTask`` launches the new instance while
the old one still holds the single-instance mutex. The new instance finds it
held and exits -- CLEANLY, exit code 0 -- and moments later the old one dies
too. Net result: the service is stopped, Task Scheduler records
``LastTaskResult: 0``, and the dashboard reports "Restarted screen sync."

Measured on 2026-09-09, reproduced twice:

    02:08:25.2  task=Running  syncPids=16336,30532
    02:08:25.8  WARNING Another MatterLights sync loop is already running
    02:08:26.9  task=Ready    syncPids=-          <- and it never comes back

The single-instance mutex is what converts the race into a SILENT outage:
without it you would get two loops fighting, which is at least visible.

These tests are deliberately platform-independent -- ``WindowsServiceControl``
is pure Python over ``subprocess`` -- so the guarantee is checked from the Linux
side too, where the underlying bug can never be observed.
"""

from __future__ import annotations

import logging
import unittest

from matterlights.service_control import SYNC, ServiceStatus
from matterlights.service_control.windows import WindowsServiceControl


class _RecordingControl(WindowsServiceControl):
    """Records the ORDER of stop/status/start without touching PowerShell."""

    def __init__(self, states: list[str]) -> None:
        super().__init__(logging.getLogger("test.service_control"))
        self._states = list(states)
        self.calls: list[str] = []

    def stop(self, service: str) -> int:
        self.calls.append("stop")
        return 1

    def start(self, service: str) -> None:
        self.calls.append("start")

    def status(self, service: str) -> ServiceStatus:
        self.calls.append("status")
        state = self._states.pop(0) if self._states else "Ready"
        return ServiceStatus(exists=True, name="MatterLights Screen Sync", state=state)

    def _sleep(self, seconds: float) -> None:
        return None

    def _run(self, script: str):
        """Nothing here may reach the real Task Scheduler.

        ⚠️ NOT PARANOIA -- THIS ALREADY HAPPENED. While these tests were red,
        ``restart`` still built one inline
        ``Stop-ScheduledTask ; Start-ScheduledTask`` string and called ``_run``
        directly, sailing straight past the overrides above. The red run
        therefore stopped and started the REAL sync service three times, once
        per test, and left it dead -- visible in the log as three
        "Another MatterLights sync loop is already running" warnings 0.3s apart.

        Failing loudly here keeps the suite off the live machine AND doubles as
        the guard: reimplementing restart as one inline PowerShell string, which
        is exactly the bug, trips this instead of silently bouncing the service.
        """

        raise AssertionError(
            "service_control tests must not shell out to PowerShell; "
            f"restart() should compose stop()/status()/start(). Got: {script!r}"
        )


class RestartWaitsForStopTest(unittest.TestCase):
    def test_start_happens_only_after_the_service_stops_running(self) -> None:
        control = _RecordingControl(["Running", "Running", "Ready"])

        control.restart(SYNC)

        self.assertEqual(["stop", "status", "status", "status", "start"], control.calls)

    def test_a_service_already_stopped_starts_immediately(self) -> None:
        control = _RecordingControl(["Ready"])

        control.restart(SYNC)

        self.assertEqual(["stop", "status", "start"], control.calls)

    def test_it_gives_up_waiting_rather_than_hanging_forever(self) -> None:
        """A service that never reports stopped must not wedge the request."""

        control = _RecordingControl(["Running"] * 50)
        control._STOP_WAIT_POLLS = 3

        control.restart(SYNC)

        self.assertEqual(["stop", "status", "status", "status", "start"], control.calls)

    def test_the_wait_reports_whether_it_actually_stopped(self) -> None:
        stopped = _RecordingControl(["Ready"])
        never = _RecordingControl(["Running"] * 50)
        never._STOP_WAIT_POLLS = 2

        self.assertTrue(stopped._wait_until_stopped(SYNC))
        self.assertFalse(never._wait_until_stopped(SYNC))


if __name__ == "__main__":
    unittest.main()
