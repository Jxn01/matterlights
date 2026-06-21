from __future__ import annotations

import sys
import unittest

from matterlights.process_lock import acquire_sync_singleton


@unittest.skipUnless(sys.platform == "win32", "named-mutex single-instance guard is Windows-only")
class ProcessLockTests(unittest.TestCase):
    def test_second_acquisition_is_blocked_until_the_first_releases(self) -> None:
        # Use an isolated mutex name so a real running sync loop does not interfere.
        name = "MatterLightsScreenSyncSingletonTest"
        first = acquire_sync_singleton(name=name)
        self.assertIsNotNone(first)
        try:
            # A second loop (even in-process) must be turned away.
            self.assertIsNone(acquire_sync_singleton(name=name))
        finally:
            first.release()

        # Once released, the lock is available again.
        third = acquire_sync_singleton(name=name)
        self.assertIsNotNone(third)
        third.release()


if __name__ == "__main__":
    unittest.main()
