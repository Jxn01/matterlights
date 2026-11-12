"""The TV as a frame source: what each packet does to the picture, and what silence does."""
from __future__ import annotations

import socket
import struct
import sys
import time
import unittest

from matterlights import capture
from matterlights.capture import TvSource, rgb_to_bgrx
from matterlights.capture.tv import (
    FLAG_FRAMES,
    FLAG_IDLE,
    FLAG_PROTECTED,
    MAGIC,
    STALE_SECONDS,
    WARM_RGB,
    TvCaptureBackend,
    parse_packet,
    solid_bgrx,
)
from matterlights.screen import sample_zone_samples_from_screenshot, ScreenZone

TV = "192.168.10.10"


def packet(flag, width=0, height=0, rgb=b"", seq=1):
    return MAGIC + struct.pack("!IHHB", seq, width, height, flag) + rgb


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Parsing(unittest.TestCase):
    def test_a_frame_reads_back(self):
        self.assertEqual(parse_packet(packet(FLAG_FRAMES, 1, 2, bytes(6))), (FLAG_FRAMES, 1, 2, bytes(6)))

    def test_the_real_companion_is_understood(self):
        """The TV's own packet builder, not a copy of its format: the two repos must not drift."""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "tv-hub/tv/jxn-hub-agent.py"
        if not path.exists():
            self.skipTest("the tv-hub repo is not beside this one")
        if sys.platform == "win32":
            # The agent is the TV's Linux program and imports pty (termios). The packet
            # format does not depend on the OS, so the Linux run of this test covers it.
            self.skipTest("the TV agent imports pty, which Windows has no equivalent of")
        spec = importlib.util.spec_from_file_location("agent", path)
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        self.assertEqual(parse_packet(agent.frame_packet(9, "frames", 2, 1, bytes(6)))[:3], (FLAG_FRAMES, 2, 1))
        self.assertEqual(parse_packet(agent.frame_packet(9, "protected"))[0], FLAG_PROTECTED)
        self.assertEqual(parse_packet(agent.frame_packet(9, "idle"))[0], FLAG_IDLE)

    def test_junk_is_refused(self):
        for junk in (b"", b"JXNF", b"XXXX" + bytes(20), packet(FLAG_FRAMES, 2, 2, bytes(6)), packet(FLAG_FRAMES), packet(7)):
            self.assertIsNone(parse_packet(junk), junk)


class Grabbing(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.tv = TvCaptureBackend(TvSource("127.0.0.1", free_port(), TV), clock=lambda: self.now)
        self.addCleanup(self.tv.close)

    def pixel(self):
        raw, _, _ = self.tv.grab("primary")
        return tuple(raw[:3])

    def test_silence_from_the_start_is_black(self):
        self.assertEqual(self.pixel(), (0, 0, 0))

    def test_a_frame_arrives_as_bgrx(self):
        self.tv.receive(packet(FLAG_FRAMES, 2, 1, bytes([10, 20, 30, 40, 50, 60])), TV)
        raw, width, height = self.tv.grab("primary")
        self.assertEqual((width, height), (2, 1))
        self.assertEqual(raw, bytes([30, 20, 10, 0, 60, 50, 40, 0]))

    def test_no_new_frame_keeps_the_last_one(self):
        self.tv.receive(packet(FLAG_FRAMES, 1, 1, bytes([9, 8, 7])), TV)
        self.now += STALE_SECONDS - 1
        self.assertEqual(self.pixel(), (7, 8, 9))

    def test_a_silent_tv_goes_black(self):
        self.tv.receive(packet(FLAG_FRAMES, 1, 1, bytes([9, 8, 7])), TV)
        self.now += STALE_SECONDS + 1
        self.assertEqual(self.pixel(), (0, 0, 0))

    def test_idle_is_black_and_protected_is_warm(self):
        self.tv.receive(packet(FLAG_FRAMES, 1, 1, bytes([9, 8, 7])), TV)
        self.tv.receive(packet(FLAG_IDLE), TV)
        self.assertEqual(self.pixel(), (0, 0, 0))
        self.tv.receive(packet(FLAG_PROTECTED), TV)
        red, green, blue = WARM_RGB
        self.assertEqual(self.pixel(), (blue, green, red))

    def test_only_the_tv_is_believed(self):
        self.tv.receive(packet(FLAG_FRAMES, 1, 1, bytes([9, 8, 7])), "192.168.10.66")
        self.assertEqual(self.pixel(), (0, 0, 0))

    def test_a_bad_packet_does_not_count_as_hearing_the_tv(self):
        self.tv.receive(packet(FLAG_FRAMES, 1, 1, bytes([9, 8, 7])), TV)
        self.now += STALE_SECONDS - 1
        self.tv.receive(b"nonsense", TV)
        self.now += 2
        self.assertEqual(self.pixel(), (0, 0, 0))

    def test_it_reports_one_screen(self):
        self.assertEqual([m.name for m in self.tv.list_monitors()], ["LG TV"])

    def test_a_png_comes_out(self):
        png, width, height = self.tv.grab_png("primary")
        self.assertTrue(png.startswith(bytes.fromhex("89504e47")))
        self.assertEqual((width, height), (64, 36))


class OverTheNetwork(unittest.TestCase):
    def test_a_real_datagram_gets_through_and_close_lets_go_of_the_port(self):
        port = free_port()
        tv = TvCaptureBackend(TvSource("127.0.0.1", port, "127.0.0.1"))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(packet(FLAG_FRAMES, 1, 1, bytes([1, 2, 3])), ("127.0.0.1", port))
            deadline = time.monotonic() + 3
            while tv.grab("primary")[1:] != (1, 1) and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(tv.grab("primary")[0], bytes([3, 2, 1, 0]))
        tv.close()
        TvCaptureBackend(TvSource("127.0.0.1", port, "127.0.0.1")).close()


class WhatTheSyncLoopSees(unittest.TestCase):
    """The reason for the design: these go through the same arithmetic as a screen."""

    def sample(self, raw, width, height):
        zone = ScreenZone("full", 0.0, 0.0, 1.0, 1.0)
        return sample_zone_samples_from_screenshot(raw, width, height, 1, 1.0, (zone,))[0]

    def test_black_turns_lights_off(self):
        self.assertTrue(self.sample(solid_bgrx((0, 0, 0), 64, 36), 64, 36).should_turn_off(12, 0.05))

    def test_warm_does_not(self):
        self.assertFalse(self.sample(solid_bgrx(WARM_RGB, 64, 36), 64, 36).should_turn_off(12, 0.05))

    def test_a_small_frame_is_a_picture(self):
        rgb = bytes([200, 40, 40]) * (64 * 36)
        self.assertFalse(self.sample(rgb_to_bgrx(rgb), 64, 36).should_turn_off(12, 0.05))


class Choosing(unittest.TestCase):
    def test_a_tv_source_replaces_the_screen(self):
        source = TvSource("127.0.0.1", free_port(), TV)
        capture.configure(0.2, "primary", source)
        self.addCleanup(capture.configure, 0.2, "", None)
        backend = capture.get_backend()
        self.assertIsInstance(backend, TvCaptureBackend)


if __name__ == "__main__":
    unittest.main()
