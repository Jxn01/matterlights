"""Linux capture: geometry, frame reuse, the rate cap, and session rebuilds.

The GStreamer pipeline is stubbed out throughout. What is being tested here is
the logic around it -- which target resolves to which rectangle, what happens
when no new frame arrives, and whether an invalidated session actually rebuilds.
Whether frames flow is not a unit-testable property; that is verified live.
"""

from __future__ import annotations

import sys
import unittest

if sys.platform == "win32":  # pragma: no cover - Linux backend, Linux tests
    raise unittest.SkipTest("Linux capture backend")

from matterlights.capture import Monitor
from matterlights.capture.linux import (
    LinuxCaptureBackend,
    build_pipeline_description,
    caps_framerate,
)
from matterlights.capture.mutter import parse_monitors, resolve_from_monitors

from fixtures.mutter_display_state import DISPLAY_STATE


class MonitorGeometryTest(unittest.TestCase):
    """Against the real recorded state of this rig's three screens."""

    def setUp(self) -> None:
        self.monitors = parse_monitors(DISPLAY_STATE)

    def test_indexes_are_ordered_by_position_not_by_mutter_order(self) -> None:
        names = [monitor.name for monitor in self.monitors[1:]]
        lefts = [monitor.left for monitor in self.monitors[1:]]
        self.assertEqual(sorted(lefts), lefts)
        self.assertEqual(["HDMI-1", "DP-2", "DP-1"], names)

    def test_scale_is_divided_out(self) -> None:
        # DP-2 is 3840x2160 at scale 1.0 -- unchanged.
        dp2 = next(m for m in self.monitors if m.name == "DP-2")
        self.assertEqual((3840, 2160), (dp2.width, dp2.height))

    def test_rotated_panels_swap_axes_after_scaling(self) -> None:
        # HDMI-1 is a 3840x2160 panel at scale 1.25 with a quarter turn:
        # 3072x1728 logical, then swapped.
        hdmi = next(m for m in self.monitors if m.name == "HDMI-1")
        self.assertEqual((1728, 3072), (hdmi.width, hdmi.height))

    def test_index_zero_is_the_bounding_box(self) -> None:
        box = self.monitors[0]
        self.assertEqual(0, box.index)
        self.assertEqual(
            max(m.left + m.width for m in self.monitors[1:]) - min(m.left for m in self.monitors[1:]),
            box.width,
        )

    def test_primary_resolves_to_the_flagged_monitor(self) -> None:
        self.assertEqual("DP-2", resolve_from_monitors(self.monitors, "primary").name)

    def test_all_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_from_monitors(self.monitors, "all"))

    def test_missing_index_raises_before_any_recording_starts(self) -> None:
        with self.assertRaises(ValueError) as caught:
            resolve_from_monitors(self.monitors, "9")
        self.assertIn("not available", str(caught.exception))


class CapsFramerateTest(unittest.TestCase):
    """R3: the cap is derived from the sync interval, never hard-coded."""

    def test_twice_the_sampling_rate(self) -> None:
        self.assertEqual(10, caps_framerate(0.2))
        self.assertEqual(4, caps_framerate(0.5))
        self.assertEqual(2, caps_framerate(1.0))

    def test_never_below_one(self) -> None:
        self.assertEqual(1, caps_framerate(10.0))
        self.assertEqual(1, caps_framerate(0.0))

    def test_the_cap_reaches_the_pipeline(self) -> None:
        self.assertIn("max-framerate=10/1", build_pipeline_description(7, 0.2))
        self.assertIn("max-framerate=4/1", build_pipeline_description(7, 0.5))

    def test_pipeline_requests_bgrx_and_a_non_blocking_sink(self) -> None:
        description = build_pipeline_description(42, 0.2)
        self.assertIn("pipewiresrc path=42", description)
        self.assertIn("format=BGRx", description)
        self.assertIn("drop=true", description)
        self.assertIn("emit-signals=true", description)


class FakeSource:
    """Stands in for MutterSource without touching D-Bus."""

    def __init__(self, monitors: list[Monitor], present: set[str] | None = None) -> None:
        self.monitors = monitors
        self.present = present if present is not None else {m.name for m in monitors[1:]}
        self.opened: list[str] = []
        self.active_connector = ""
        self.closed = 0
        self._invalidate = None
        self.node = 100

    def set_invalidation_callback(self, callback) -> None:
        self._invalidate = callback

    def list_monitors(self) -> list[Monitor]:
        return self.monitors

    def connector_present(self, connector: str) -> bool:
        return connector in self.present

    def open_stream(self, capture_target: str) -> int:
        monitor = resolve_from_monitors(self.monitors, capture_target)
        self.active_connector = monitor.name if monitor else "all"
        self.opened.append(capture_target)
        self.node += 1
        return self.node

    def close(self) -> None:
        self.closed += 1

    def invalidate(self, reason: str = "test") -> None:
        assert self._invalidate is not None
        self._invalidate(reason)


class _StubbedBackend(LinuxCaptureBackend):
    """Backend with the GStreamer pipeline replaced by a counter.

    Starting the pipeline delivers a frame, because that is the real order:
    ``_ensure_stream`` clears the buffer and builds the stream, and only then can
    a frame arrive. Feeding beforehand would test a sequence that cannot happen.
    """

    def __init__(self, source) -> None:
        self.started: list[int] = []
        self.next_frame: tuple[bytes, int, int] | None = (b"\x01\x02\x03\x00", 1, 1)
        super().__init__(source=source)

    def _start_pipeline(self, node_id: int) -> None:
        self.started.append(node_id)
        self._pipeline = object()
        if self.next_frame is not None:
            self.feed(*self.next_frame)

    def _teardown_pipeline(self) -> None:
        self._pipeline = None

    def feed(self, raw: bytes, width: int, height: int) -> None:
        with self._lock:
            self._latest = (raw, width, height)
        self._first_frame.set()


class BackendBehaviourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.monitors = parse_monitors(DISPLAY_STATE)
        self.source = FakeSource(self.monitors)
        self.backend = _StubbedBackend(self.source)

    def test_grab_returns_the_latest_frame(self) -> None:
        self.backend.next_frame = (b"\x01\x02\x03\x00", 1, 1)
        self.assertEqual((b"\x01\x02\x03\x00", 1, 1), self.backend.grab("primary"))

    def test_grab_sees_a_frame_delivered_after_the_stream_started(self) -> None:
        self.backend.grab("primary")
        self.backend.feed(b"\xaa\xbb\xcc\x00", 1, 1)
        self.assertEqual((b"\xaa\xbb\xcc\x00", 1, 1), self.backend.grab("primary"))

    def test_grab_reuses_the_previous_frame_when_none_arrived(self) -> None:
        """A static desktop produces no frames. That is normal, not an error.

        Reporting black here would blink the lights off whenever the user stopped
        moving the mouse -- the same reasoning as dxcam returning None on Windows.
        """

        self.backend.next_frame = (b"\x09\x09\x09\x00", 1, 1)
        first = self.backend.grab("primary")
        second = self.backend.grab("primary")
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.backend.started), "no new stream should be built")

    def test_switching_target_rebuilds_the_stream(self) -> None:
        self.backend.grab("primary")
        self.backend.grab("3")
        self.assertEqual(["primary", "3"], self.source.opened)

    def test_invalidation_rebuilds_exactly_once(self) -> None:
        """R4: a closed session or a changed layout must rebuild, and only once."""

        self.backend.grab("primary")
        self.source.invalidate("monitor layout changed")
        self.backend.grab("primary")
        self.backend.grab("primary")
        self.assertEqual(2, len(self.backend.started))

    def test_unplugged_screen_falls_back_and_keeps_asking_for_the_original(self) -> None:
        """R4: the connector check is what makes this fire.

        Re-resolving the index would not: unplug the middle screen of three and
        index 2 still resolves happily, just to a different monitor.
        """

        self.backend.set_fallback_target("primary")
        self.backend.grab("3")
        self.assertEqual("DP-1", self.source.active_connector)

        self.source.present.discard("DP-1")
        self.source.invalidate("monitor layout changed")
        self.backend.grab("3")

        self.assertEqual(["3", "primary"], self.source.opened)
        self.assertEqual("3", self.backend._active_target, "the request must be remembered")

    def test_close_closes_the_source(self) -> None:
        self.backend.close()
        self.assertEqual(1, self.source.closed)


if __name__ == "__main__":
    unittest.main()
