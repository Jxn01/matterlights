"""The Home Assistant writes must never stall the capture loop."""

from __future__ import annotations

import logging
import threading
import time
import unittest
from types import SimpleNamespace

from matterlights.light_worker import LightWorker


def _state(entity_id):
    return SimpleNamespace(entity_id=entity_id, update=SimpleNamespace(entity_id=entity_id))


class _Client:
    """A Home Assistant client that can block, like a bulb that stopped answering."""

    def __init__(self, delay: float = 0.0, failed=None, raises: bool = False) -> None:
        self.delay = delay
        self.failed = failed or []
        self.raises = raises
        self.calls = 0
        self.started = threading.Event()

    def apply_light_updates(self, updates, transition_seconds):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("home assistant said no")
        return list(self.failed)


class LightWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def _worker(self, client):
        worker = LightWorker(client, logging.getLogger("test"))
        self.addCleanup(worker.stop)
        return worker

    def test_submit_returns_immediately_even_when_the_call_blocks(self) -> None:
        """🚨 The whole point: a bulb timing out froze capture for 3 seconds."""

        client = _Client(delay=1.0)
        worker = self._worker(client)
        started = time.monotonic()
        worker.submit([_state("light.a")], 0.0)
        self.assertLess(time.monotonic() - started, 0.1, "submit must not wait for the network")

    def test_the_result_comes_back_on_collect(self) -> None:
        client = _Client(failed=["light.b"])
        worker = self._worker(client)
        worker.submit([_state("light.a"), _state("light.b")], 0.0)
        deadline = time.monotonic() + 3
        results = []
        while not results and time.monotonic() < deadline:
            results = worker.collect()
            time.sleep(0.02)
        self.assertEqual(len(results), 1)
        states, failed = results[0]
        self.assertEqual(failed, ["light.b"])
        self.assertEqual([s.entity_id for s in states], ["light.a", "light.b"])

    def test_collect_is_empty_until_there_is_something(self) -> None:
        self.assertEqual(self._worker(_Client()).collect(), [])

    def test_latest_wins_rather_than_queueing(self) -> None:
        """A backlog of light updates is worthless -- by the time a stale one was
        sent, the screen has moved on."""

        client = _Client(delay=0.4)
        worker = self._worker(client)
        worker.submit([_state("first")], 0.0)
        client.started.wait(2)
        for name in ("second", "third", "fourth"):
            worker.submit([_state(name)], 0.0)
        deadline = time.monotonic() + 3
        seen = []
        while len(seen) < 2 and time.monotonic() < deadline:
            seen.extend(worker.collect())
            time.sleep(0.02)
        self.assertEqual(len(seen), 2, "three queued submissions must collapse to one")
        self.assertEqual([s.entity_id for s in seen[1][0]], ["fourth"])

    def test_an_exception_marks_every_light_failed_and_keeps_the_worker_alive(self) -> None:
        client = _Client(raises=True)
        worker = self._worker(client)
        worker.submit([_state("light.a")], 0.0)
        deadline = time.monotonic() + 3
        results = []
        while not results and time.monotonic() < deadline:
            results = worker.collect()
            time.sleep(0.02)
        self.assertEqual(results[0][1], ["light.a"])
        # Still working afterwards.
        client.raises = False
        worker.submit([_state("light.c")], 0.0)
        deadline = time.monotonic() + 3
        more = []
        while not more and time.monotonic() < deadline:
            more = worker.collect()
            time.sleep(0.02)
        self.assertEqual(more[0][1], [])

    def test_busy_reports_work_in_flight(self) -> None:
        client = _Client(delay=0.4)
        worker = self._worker(client)
        worker.submit([_state("light.a")], 0.0)
        client.started.wait(2)
        self.assertTrue(worker.busy)

    def test_an_empty_submission_is_ignored(self) -> None:
        client = _Client()
        worker = self._worker(client)
        worker.submit([], 0.0)
        time.sleep(0.1)
        self.assertEqual(client.calls, 0)

    def test_stop_is_prompt_and_idempotent(self) -> None:
        worker = LightWorker(_Client(), logging.getLogger("test"))
        started = time.monotonic()
        worker.stop()
        worker.stop()
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
