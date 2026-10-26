"""capture/fullscreen_watch.py: exclusive fullscreen is logged with what the capture delivered, and a starved
Desktop Duplication is re-created (bounded). Owner's A/B 2026-10-25: borderless kept sync, exclusive went black."""

import unittest

from matterlights.capture import fullscreen_watch as fw


class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, msg, *args):
        self.lines.append(msg % args)


def watch():
    info, warn = Log(), Log()
    return fw.FullscreenWatch(log=info, warn=warn), info, warn


BLACK = bytes(16 * 9 * 4)
WHITE = bytes([255]) * (16 * 9 * 4)


class Brightness(unittest.TestCase):
    def test_black_white_and_bad_input(self):
        self.assertEqual(fw.mean_brightness(BLACK, 16, 9), 0)
        self.assertEqual(fw.mean_brightness(WHITE, 16, 9), 255)
        self.assertIsNone(fw.mean_brightness(b"", 16, 9))
        self.assertIsNone(fw.mean_brightness(BLACK, 0, 0))


class Watch(unittest.TestCase):
    def test_state_changes_are_logged_with_the_delivery(self):
        w, info, _ = watch()
        w.on_state(0, 5)
        w.on_frame(0.5, WHITE, 16, 9, fresh=True, backend="dxgi")
        w.on_state(1.0, fw.QUNS_D3D_FULLSCREEN)
        self.assertTrue(w.exclusive)
        self.assertIn("not polled yet -> 5 = normal desktop", info.lines[0])
        self.assertIn("5 = normal desktop -> 3 = exclusive D3D fullscreen", info.lines[-1])
        self.assertIn("1 fresh, 0 reused", info.lines[-1])
        self.assertIn("16x9 via dxgi", info.lines[-1])
        self.assertIn("255.0/255", info.lines[-1])
        w.on_state(2.0, fw.QUNS_D3D_FULLSCREEN)            # no change, no line
        self.assertEqual(len(info.lines), 2)

    def test_starved_duplication_is_recreated_at_most_max_times(self):
        w, info, warn = watch()
        w.on_state(0, fw.QUNS_D3D_FULLSCREEN)
        w.on_frame(0, BLACK, 16, 9, fresh=True, backend="dxgi")
        asks = [t for t in range(1, 60) if w.on_frame(float(t), BLACK, 16, 9, fresh=False, backend="dxgi")]
        self.assertEqual(asks, [5, 10, 15])                # every STARVE_SECONDS, MAX_RECREATES times
        self.assertEqual(len(warn.lines), fw.MAX_RECREATES)
        self.assertTrue(any("Exclusive fullscreen:" in l and "reused" in l for l in info.lines), "periodic summary")

    def test_fresh_frames_or_borderless_never_recreate(self):
        w, _, warn = watch()
        w.on_state(0, fw.QUNS_D3D_FULLSCREEN)
        self.assertFalse(any(w.on_frame(float(t), BLACK, 16, 9, fresh=True, backend="dxgi") for t in range(60)))
        w2, _, _ = watch()
        w2.on_state(0, 2)                                   # busy: borderless fullscreen
        self.assertFalse(any(w2.on_frame(float(t), BLACK, 16, 9, fresh=False, backend="dxgi") for t in range(60)))
        w3, _, _ = watch()
        w3.on_state(0, fw.QUNS_D3D_FULLSCREEN)              # GDI has no camera to re-create
        self.assertFalse(any(w3.on_frame(float(t), BLACK, 16, 9, fresh=False, backend="gdi") for t in range(60)))
        self.assertEqual(warn.lines, [])

    def test_a_new_episode_resets_the_budget(self):
        w, _, _ = watch()
        w.on_state(0, fw.QUNS_D3D_FULLSCREEN)
        for t in range(1, 30):
            w.on_frame(float(t), BLACK, 16, 9, fresh=False, backend="dxgi")
        w.on_state(30, 5)
        w.on_state(31, fw.QUNS_D3D_FULLSCREEN)
        self.assertTrue(w.on_frame(32.0, BLACK, 16, 9, fresh=False, backend="dxgi"))

    def test_poll_rate_and_unknown_state(self):
        w, info, _ = watch()
        self.assertTrue(w.should_poll(0))
        w.on_state(0, None)
        self.assertFalse(w.should_poll(0.5))
        self.assertTrue(w.should_poll(1.0))
        self.assertIn("unknown (the shell query failed)", info.lines[-1])


if __name__ == "__main__":
    unittest.main()
