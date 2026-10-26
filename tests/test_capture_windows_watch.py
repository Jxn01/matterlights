"""The Windows backend's grab loop with ctypes.windll, mss and dxcam stubbed, so it runs on Linux: under exclusive
fullscreen a starved Desktop Duplication camera is RELEASED and rebuilt (dxcam hands an unreleased instance straight
back from create(), so forgetting it is not enough), and the GDI fallback never asks for that."""

import ctypes
import importlib
import sys
import types
import unittest
from unittest import mock


class FakeCamera:
    def __init__(self, frames):
        self.frames, self.released = frames, False

    def grab(self):
        return self.frames.pop(0) if self.frames else None

    def release(self):
        self.released = True


class Frame:
    shape = (9, 16, 4)

    def tobytes(self):
        return bytes(16 * 9 * 4)


def load(state, cameras):
    shell = types.SimpleNamespace(SHQueryUserNotificationState=lambda p: (setattr(p._obj, "value", state), 0)[1])
    windll = types.SimpleNamespace(shell32=shell, user32=types.SimpleNamespace(GetSystemMetrics=lambda i: 16 if i == 0 else 9))
    fake_mss = types.ModuleType("mss")

    class MSS:
        monitors = [{"left": 0, "top": 0, "width": 16, "height": 9},
                    {"left": 0, "top": 0, "width": 16, "height": 9, "is_primary": True}]

        def grab(self, region):
            return types.SimpleNamespace(raw=bytes(16 * 9 * 4), width=16, height=9)

        def close(self):
            pass

    fake_mss.MSS = MSS
    fake_dxcam = types.ModuleType("dxcam")
    fake_dxcam.output_info = lambda: "Device[0] Output[0]: Res:(16, 9) Rot:0 Primary:True"
    fake_dxcam.create = lambda **k: cameras.pop(0)
    patches = [mock.patch.object(ctypes, "windll", windll, create=True),
               mock.patch.dict(sys.modules, {"mss": fake_mss, "dxcam": fake_dxcam})]
    for p in patches:
        p.start()
    sys.modules.pop("matterlights.capture.windows", None)
    module = importlib.import_module("matterlights.capture.windows")
    return module, patches


class StarvedCamera(unittest.TestCase):
    def tearDown(self):
        for p in self.patches:
            p.stop()
        sys.modules.pop("matterlights.capture.windows", None)

    def test_exclusive_and_starved_releases_and_rebuilds(self):
        first, second = FakeCamera([Frame()]), FakeCamera([Frame()])
        win, self.patches = load(3, [first, second])
        backend = win.WindowsCaptureBackend()
        clock = iter(float(t) for t in range(100))
        with mock.patch.object(win.time, "monotonic", lambda: next(clock)):
            for _ in range(7):         # t=0 fresh; t=1..5 reused -> rebuilt at t=5; t=6 builds the new camera
                raw, w, h = backend.grab("primary")
        self.assertTrue(first.released, "the starved camera was released")
        self.assertEqual(win._DXCAM_CAMERAS.get((0, 0)), second, "and a new one built on the next grab")
        self.assertEqual((w, h), (16, 9))

    def test_normal_desktop_never_rebuilds_a_quiet_camera(self):
        first = FakeCamera([Frame()])
        win, self.patches = load(5, [first])
        backend = win.WindowsCaptureBackend()
        clock = iter(float(t) for t in range(100))
        with mock.patch.object(win.time, "monotonic", lambda: next(clock)):
            for _ in range(30):
                backend.grab("primary")
        self.assertFalse(first.released, "a static desktop gives no new frames; that is not starvation")


if __name__ == "__main__":
    unittest.main()
