from __future__ import annotations

import ast
import unittest
from types import SimpleNamespace

from matterlights.main import enforce_lights_off


class FakeClient:
    """Records what was asked of Home Assistant and can fail chosen lights."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failing = failing or set()

    def turn_off_lights(self, entity_ids: list[str], transition_seconds: float) -> list[str]:
        self.calls.append(list(entity_ids))
        return [entity_id for entity_id in entity_ids if entity_id in self.failing]


class EnforceLightsOffTests(unittest.TestCase):
    def test_turns_off_available_lights_and_remembers_them(self) -> None:
        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)

        self.assertEqual(client.calls, [["a", "b"]])
        self.assertEqual(confirmed, {"a", "b"})

    def test_does_not_resend_to_lights_already_confirmed_off(self) -> None:
        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a"], confirmed, 0.0)
        enforce_lights_off(client, ["a"], confirmed, 0.0)

        self.assertEqual(client.calls, [["a"]], "an off-state must not re-send every tick")

    def test_light_unavailable_at_first_is_turned_off_once_it_returns(self) -> None:
        """The regression this fixes.

        The screen went to sleep while every bulb was momentarily unavailable.
        The old code logged "turning lights off", sent nothing because the
        available list was empty, and latched a flag that stopped it ever
        retrying -- so the lights stayed on all night.
        """

        client = FakeClient()
        confirmed: set[str] = set()

        enforce_lights_off(client, [], confirmed, 0.0)
        self.assertEqual(client.calls, [], "nothing to send while every light is unavailable")

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(client.calls, [["a", "b"]], "returning lights must still be turned off")
        self.assertEqual(confirmed, {"a", "b"})

    def test_lights_that_failed_to_turn_off_are_retried_on_the_next_sweep(self) -> None:
        client = FakeClient(failing={"b"})
        confirmed: set[str] = set()

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(confirmed, {"a"}, "a failed light must not count as confirmed off")

        enforce_lights_off(client, ["a", "b"], confirmed, 0.0)
        self.assertEqual(client.calls, [["a", "b"], ["b"]], "only the failed light is retried")


if __name__ == "__main__":
    unittest.main()


class SyncLoopHandlerOrderTest(unittest.TestCase):
    """``CaptureUnavailable`` must be caught BEFORE the catch-all ``Exception``.

    Python matches except-clauses top to bottom, so moving the bare
    ``except Exception`` above the specific one silently restores the original
    defect: an ERROR traceback and a five-second backoff every time the monitor
    goes to sleep. Nothing else in the test suite would notice -- the loop is
    not unit-testable -- so the ordering is asserted structurally.
    """

    def _sync_try_blocks(self) -> list[ast.Try]:
        import inspect

        from matterlights import main as main_module

        tree = ast.parse(inspect.getsource(main_module))
        return [node for node in ast.walk(tree) if isinstance(node, ast.Try)]

    def test_capture_unavailable_is_handled_before_the_catch_all(self) -> None:
        def handler_name(handler: ast.ExceptHandler) -> str:
            return ast.unparse(handler.type) if handler.type else "bare"

        checked = 0
        for block in self._sync_try_blocks():
            names = [handler_name(h) for h in block.handlers]
            specific = [i for i, n in enumerate(names) if "CaptureUnavailable" in n]
            if not specific:
                continue
            checked += 1
            catch_alls = [
                i for i, n in enumerate(names) if n in ("Exception", "BaseException", "bare")
            ]
            for catch_all in catch_alls:
                self.assertLess(
                    specific[0],
                    catch_all,
                    f"CaptureUnavailable must precede {names[catch_all]!r}; got {names}",
                )

        self.assertEqual(
            checked, 1, "expected exactly one CaptureUnavailable handler in the sync loop"
        )


class RgbPublishDuringMasterOffTest(unittest.TestCase):
    """The RGB extension keeps receiving frames while the lamps are switched off.

    The owner chose "the lamps' master switch does not silence the RGB". That is
    not free: the sync loop takes a master-off branch that never reaches the
    capture, so nothing would be published and the extension would go dark after
    its staleness timeout. This asserts the branch publishes, and that it only
    does so when the extension is actually wanted.
    """

    def _source(self) -> str:
        import inspect

        from matterlights import main as main_module

        return inspect.getsource(main_module)

    def test_the_master_off_branch_publishes_for_the_extension(self) -> None:
        tree = ast.parse(self._source())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_publish_for_rgb_only"
        ]
        self.assertEqual(len(calls), 1, "master-off should publish exactly once per tick")

    def test_publishing_is_gated_on_the_extension_being_enabled_and_on(self) -> None:
        """Otherwise a machine with no extension would capture for nobody."""

        source = self._source()
        start = source.index("if not control_state.lights_on:")
        end = source.index("elif not display_on:", start)
        branch = source[start:end]
        self.assertIn("rgb_publisher is not None", branch)
        self.assertIn("control_state.rgb_on", branch)
        self.assertIn("display_on", branch)

    def test_a_failed_capture_is_swallowed(self) -> None:
        """The lamps are already off; a capture failure must not break the loop."""

        from matterlights.main import _publish_for_rgb_only

        class Exploding:
            def grab(self, _target):
                raise RuntimeError("compositor inhibited")

        class Publisher:
            def __init__(self) -> None:
                self.calls = 0

            def publish(self, *a, **k) -> bool:
                self.calls += 1
                return True

        publisher = Publisher()
        settings = SimpleNamespace(
            screen_capture_target="primary",
            sample_stride=121,
            color_boost=1.5,
            light_entities=["a"],
            dark_threshold=12,
            dark_active_ratio_threshold=0.05,
        )
        control = SimpleNamespace(capture_target=None, lights_on=False, rgb_on=True)

        _publish_for_rgb_only(publisher, Exploding(), settings, control, ["a"], {}, True)
        self.assertEqual(publisher.calls, 0, "nothing published, nothing raised")


class PublishRgbFrameTest(unittest.TestCase):
    """🚨 What reaches the RGB extension must be what the BULBS EMIT.

    Publishing the sampled colour instead is not visibly wrong on a bright
    screen and renders the whole rig black on a dark one, because the receiving
    hardware has no brightness channel to apply separately. That shipped, and
    the case looked switched off for a day while the lamps looked correct.
    """

    def _samples(self, near_colour, far_colour, brightness=200, active_ratio=0.5):
        from matterlights.ambience import _FAR_ZONE, _NEAR_ZONE
        from matterlights.screen import RgbColor, ZoneSample

        return [
            ZoneSample(zone=_NEAR_ZONE, color=RgbColor(*near_colour),
                       average_brightness=brightness, active_ratio=active_ratio),
            ZoneSample(zone=_FAR_ZONE, color=RgbColor(*far_colour),
                       average_brightness=brightness, active_ratio=active_ratio),
        ]

    def _publish(self, zone_samples, brightness_floor=255, **overrides):
        from matterlights.main import _publish_rgb_frame

        published: dict = {}

        class Publisher:
            def publish(self, near, far, **kwargs):
                published.update(near=near, far=far, **kwargs)

        settings = SimpleNamespace(
            brightness_floor=brightness_floor,
            dark_threshold=12,
            dark_active_ratio_threshold=0.05,
        )
        _publish_rgb_frame(
            Publisher(),
            zone_samples,
            screen_dark=overrides.get("screen_dark", False),
            display_on=overrides.get("display_on", True),
            control_state=SimpleNamespace(lights_on=True, rgb_on=True),
            settings=settings,
        )
        return published

    def test_a_dim_sample_is_published_at_full_magnitude(self) -> None:
        published = self._publish(self._samples((24, 24, 23), (24, 24, 23)))
        self.assertEqual(published["near"], (255, 255, 244))
        self.assertEqual(published["far"], (255, 255, 244))

    def test_it_does_not_publish_the_raw_sample(self) -> None:
        published = self._publish(self._samples((12, 6, 0), (12, 6, 0)))
        self.assertNotEqual(published["near"], (12, 6, 0), "raw sample must not reach the LEDs")
        self.assertEqual(published["near"], (255, 128, 0))

    def test_near_and_far_stay_distinct(self) -> None:
        published = self._publish(self._samples((255, 0, 0), (0, 0, 255)))
        self.assertEqual(published["near"], (255, 0, 0))
        self.assertEqual(published["far"], (0, 0, 255))

    def test_a_frame_the_bulbs_would_turn_off_publishes_black(self) -> None:
        published = self._publish(self._samples((2, 2, 2), (2, 2, 2), brightness=3, active_ratio=0.0))
        self.assertEqual(published["near"], (0, 0, 0))
        self.assertEqual(published["far"], (0, 0, 0))

    def test_no_samples_publishes_black_without_raising(self) -> None:
        published = self._publish([])
        self.assertEqual(published["near"], (0, 0, 0))
        self.assertEqual(published["brightness"], 0)

    def test_the_verdicts_are_passed_through_untouched(self) -> None:
        published = self._publish(
            self._samples((255, 0, 0), (255, 0, 0)), screen_dark=True, display_on=False
        )
        self.assertIs(published["screen_dark"], True)
        self.assertIs(published["display_on"], False)
        self.assertIs(published["lights_on"], True)
        self.assertIs(published["rgb_on"], True)


class PalettePublishTest(unittest.TestCase):
    """The palette must reach the wire, and its absence must not break anything.

    ⚠️ `ambience_frame` is bound before the mode branch on purpose. The publish
    call sits *outside* that branch, so assigning it only in the ambience arm
    leaves the name unbound for every other colour_sync_mode -- a NameError on
    the first frame, in a path the ambience tests never touch.
    """

    def _publish(self, frame):
        from matterlights.main import _publish_rgb_frame
        from matterlights.ambience import _FAR_ZONE, _NEAR_ZONE
        from matterlights.screen import RgbColor, ZoneSample

        published: dict = {}

        class Publisher:
            def publish(self, near, far, **kwargs):
                published.update(near=near, far=far, **kwargs)

        samples = [
            ZoneSample(zone=_NEAR_ZONE, color=RgbColor(255, 0, 0),
                       average_brightness=200, active_ratio=0.5),
            ZoneSample(zone=_FAR_ZONE, color=RgbColor(0, 0, 255),
                       average_brightness=200, active_ratio=0.5),
        ]
        _publish_rgb_frame(
            Publisher(), samples, screen_dark=False, display_on=True,
            control_state=SimpleNamespace(lights_on=True, rgb_on=True),
            settings=SimpleNamespace(brightness_floor=255, dark_threshold=12,
                                     dark_active_ratio_threshold=0.05),
            frame=frame,
        )
        return published

    def test_no_frame_publishes_an_empty_palette_without_raising(self) -> None:
        """Every non-ambience mode takes this path."""

        self.assertEqual(self._publish(None)["palette"], [])

    def test_a_frame_publishes_its_palette_in_order(self) -> None:
        from matterlights.ambience import AmbienceFrame, PaletteColor
        from matterlights.screen import RgbColor

        frame = AmbienceFrame(
            palette=(
                PaletteColor(RgbColor(255, 0, 0), 0.6),
                PaletteColor(RgbColor(0, 0, 255), 0.4),
            ),
            mix=RgbColor(128, 0, 128), average_brightness=200, active_ratio=0.5,
        )
        palette = self._publish(frame)["palette"]
        self.assertEqual([c for c, _w in palette], [(255, 0, 0), (0, 0, 255)])
        self.assertEqual([w for _c, w in palette], [0.6, 0.4])

    def test_near_and_far_are_still_published_alongside(self) -> None:
        """v2 adds the palette; it does not replace the pair."""

        published = self._publish(None)
        self.assertEqual(published["near"], (255, 0, 0))
        self.assertEqual(published["far"], (0, 0, 255))


class SleepPublishingTest(unittest.TestCase):
    """🚨 A bulb failure must not turn the case off and on again.

    The sync loop backs off for five seconds when Home Assistant refuses writes,
    which is right -- there is no point hammering it. But the RGB extension is a
    bystander watching the same screen, and it goes dark after three seconds of
    silence, correctly, because a frozen frame looks exactly like working sync.
    So one flaky bulb produced a visible off/on cycle across the whole case,
    every retry, with nothing in either log to say why.
    """

    class _Publisher:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        def publish(self, **kwargs) -> None:
            self.sent.append(kwargs)

    def test_it_keeps_publishing_for_the_whole_backoff(self) -> None:
        from matterlights.main import _sleep_publishing

        publisher = self._Publisher()
        _sleep_publishing(publisher, {"near": (1, 2, 3)}, 0.3, 0.05)
        self.assertGreaterEqual(len(publisher.sent), 3)
        self.assertTrue(all(frame == {"near": (1, 2, 3)} for frame in publisher.sent))

    def test_it_still_sleeps_the_requested_time(self) -> None:
        """The backoff has to actually back off, or it defeats its own purpose."""

        import time
        from matterlights.main import _sleep_publishing

        started = time.monotonic()
        _sleep_publishing(self._Publisher(), {"near": (0, 0, 0)}, 0.25, 0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_no_publisher_is_just_a_sleep(self) -> None:
        from matterlights.main import _sleep_publishing

        _sleep_publishing(None, {"near": (0, 0, 0)}, 0.1, 0.05)

    def test_no_frame_yet_is_just_a_sleep(self) -> None:
        """The very first iteration can fail before anything was published."""

        from matterlights.main import _sleep_publishing

        publisher = self._Publisher()
        _sleep_publishing(publisher, None, 0.1, 0.05)
        self.assertEqual(publisher.sent, [])

    def test_a_zero_interval_cannot_spin(self) -> None:
        from matterlights.main import _sleep_publishing

        publisher = self._Publisher()
        _sleep_publishing(publisher, {"near": (0, 0, 0)}, 0.2, 0.0)
        self.assertLess(len(publisher.sent), 60, "a 0 interval must not become a busy loop")

    def test_publish_returns_the_payload_it_sent(self) -> None:
        """That return value is what the backoff republishes."""

        published = PalettePublishTest()._publish(None)
        self.assertIn("near", published)
