"""The POSIX single-instance guard."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

if sys.platform == "win32":  # pragma: no cover
    raise unittest.SkipTest("POSIX flock guard")

from matterlights.process_lock import _lock_path, acquire_sync_singleton


class FlockSingletonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.runtime})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logger = logging.getLogger("test.lock")
        self.name = "matterlights-test-singleton"

    def test_first_caller_gets_a_lock(self) -> None:
        lock = acquire_sync_singleton(self.logger, self.name)
        self.addCleanup(lock.release)
        self.assertIsNotNone(lock)

    def test_lock_file_lives_in_the_runtime_dir(self) -> None:
        self.assertEqual(self.runtime, str(_lock_path(self.name).parent))

    def test_a_second_process_is_refused_while_the_first_holds_it(self) -> None:
        """Two sync loops driving the same bulbs send overlapping commands.

        Cross-process, because flock is only meaningful between processes: two
        acquisitions in one process succeed by design.
        """

        lock = acquire_sync_singleton(self.logger, self.name)
        self.addCleanup(lock.release)
        self.assertEqual("refused", self._acquire_in_subprocess())

    def test_the_kernel_releases_the_lock_when_the_holder_dies(self) -> None:
        """No stale lock after a crash -- the reason flock beats a pidfile."""

        self.assertEqual("acquired", self._acquire_in_subprocess())
        self.assertEqual("acquired", self._acquire_in_subprocess())

    def test_releasing_lets_the_next_caller_in(self) -> None:
        lock = acquire_sync_singleton(self.logger, self.name)
        lock.release()
        self.assertEqual("acquired", self._acquire_in_subprocess())

    def _acquire_in_subprocess(self) -> str:
        code = (
            "import logging;"
            "from matterlights.process_lock import acquire_sync_singleton;"
            f"lock = acquire_sync_singleton(logging.getLogger('x'), {self.name!r});"
            "print('refused' if lock is None else 'acquired')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
            env={**os.environ, "XDG_RUNTIME_DIR": self.runtime},
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
