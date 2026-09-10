"""Apply Home Assistant light updates off the capture loop.

🚨 **A bulb that stops answering must not be able to stall screen capture.**

Home Assistant writes are network calls with a multi-second timeout. Making them
inline in the sync loop meant one unreachable Matter bulb froze *everything* for
the length of that timeout: no capture, no sampling, and -- because the RGB
extension is fed from the same loop -- no frames for its consumer either. The
extension then went dark on its staleness rule, correctly, and the whole case
blanked every time a single flaky bulb timed out. Measured on 2026-09-10: two
blackouts in three minutes, each exactly as long as the Home Assistant timeout.

So the call runs here instead, and the loop never waits for it.

**What is deliberately NOT moved.** Only the network call. Every piece of shared
state -- ``last_colors``, ``off_entity_ids``, ``retry_entity_ids`` -- is read by
``_build_desired_states`` on the loop thread and written after the call returns.
Updating it from a worker would race with the next tick's read for no benefit.
The worker therefore takes an immutable request and hands back an immutable
result; the loop applies the bookkeeping itself, on its own thread, when it
collects that result.

**Latest wins.** If a request arrives while one is in flight, it replaces any
request still queued rather than joining a queue. A backlog of light updates is
worthless: by the time a stale one was sent, the screen has moved on.
"""

from __future__ import annotations

import logging
import threading


class LightWorker:
    """One background thread that applies light updates and reports results."""

    def __init__(self, client, logger: logging.Logger, name: str = "matterlights-lights") -> None:
        self._client = client
        self._logger = logger
        self._pending: tuple | None = None
        self._results: list[tuple] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._in_flight = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._in_flight or self._pending is not None

    def submit(self, states: list, transition_seconds: float) -> None:
        """Queue an update, replacing anything not yet started."""

        if not states:
            return
        with self._lock:
            self._pending = (list(states), transition_seconds)
        self._wake.set()

    def collect(self) -> list[tuple]:
        """Take every finished result: a list of ``(states, failed_entity_ids)``."""

        with self._lock:
            results, self._results = self._results, []
        return results

    def stop(self, timeout: float = 2.0) -> None:
        self._running = False
        self._wake.set()
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while self._running:
            # A timeout rather than a blocking wait, so stop() is never more than
            # half a second away even if no work ever arrives.
            self._wake.wait(0.5)
            self._wake.clear()
            with self._lock:
                job, self._pending = self._pending, None
                self._in_flight = job is not None
            if job is None:
                continue
            states, transition_seconds = job
            try:
                failed = self._client.apply_light_updates(
                    [state.update for state in states], transition_seconds
                )
            except Exception:  # noqa: BLE001 - a bulb must not kill the worker
                self._logger.exception("Light update raised; treating every light as failed")
                failed = [state.entity_id for state in states]
            with self._lock:
                self._results.append((states, list(failed)))
                self._in_flight = False
