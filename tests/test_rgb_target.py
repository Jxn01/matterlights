"""``RGB_PUBLISH_SOCKET``: a unix socket path, or ``udp://HOST:PORT``.

Windows has no unix datagram sockets -- its ``AF_UNIX`` is ``SOCK_STREAM``
only -- so the RGB extension listens on UDP there, and this setting has to say
so. A malformed value is refused when the settings load, where the message
reaches a person, rather than when the first frame is published, where it would
be one swallowed warning in a loop that must never stop.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from matterlights.rgb_target import UdpTarget, parse_rgb_target


class ParseTest(unittest.TestCase):
    def test_a_udp_url_is_a_udp_target(self) -> None:
        self.assertEqual(parse_rgb_target("udp://127.0.0.1:45731"), UdpTarget("127.0.0.1", 45731))

    def test_it_prints_back_as_the_same_url(self) -> None:
        self.assertEqual(str(parse_rgb_target("udp://127.0.0.1:45731")), "udp://127.0.0.1:45731")

    def test_the_scheme_is_case_insensitive_and_whitespace_is_ignored(self) -> None:
        self.assertEqual(parse_rgb_target("  UDP://localhost:50000 "), UdpTarget("localhost", 50000))

    def test_anything_else_is_a_path(self) -> None:
        self.assertEqual(parse_rgb_target("/run/user/1000/ambience-rgb.sock"), Path("/run/user/1000/ambience-rgb.sock"))

    def test_targets_pass_through(self) -> None:
        target = UdpTarget("127.0.0.1", 1)
        self.assertIs(parse_rgb_target(target), target)
        path = Path("/x.sock")
        self.assertIs(parse_rgb_target(path), path)

    def test_a_malformed_url_says_what_it_should_look_like(self) -> None:
        for value in (
            "udp://127.0.0.1",
            "udp://:45731",
            "udp://127.0.0.1:",
            "udp://127.0.0.1:0",
            "udp://127.0.0.1:65536",
            "udp://127.0.0.1:port",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    parse_rgb_target(value)
                self.assertIn("udp://127.0.0.1:45731", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
