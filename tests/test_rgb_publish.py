"""The RGB publisher must never be able to break the sync loop.

The lights are the product; the RGB extension is a bystander. Every test here
exists to prove one thing: whatever goes wrong on the socket, ``publish``
returns and the caller carries on. A regression that lets an exception escape
would take the bulbs down whenever the extension was stopped -- which is the
normal state on every machine except one.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - unix sockets
    raise unittest.SkipTest("unix datagram sockets")

from matterlights.rgb_publish import PAYLOAD_VERSION, AmbiencePublisher

FRAME = dict(
    brightness=42,
    active_ratio=0.25,
    screen_dark=False,
    display_on=True,
    lights_on=True,
    rgb_on=True,
)


class _Listener:
    """A bound datagram socket standing in for the extension."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(2.0)

    def receive(self) -> dict:
        return json.loads(self.sock.recv(65535).decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


class PublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rgb.sock"
        self.addCleanup(self.tmp.cleanup)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_listener_receives_the_frame(self) -> None:
        listener = _Listener(self.path)
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)

        self.assertTrue(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        payload = listener.receive()

        self.assertEqual(payload["v"], PAYLOAD_VERSION)
        self.assertEqual(payload["near"], [1, 2, 3])
        self.assertEqual(payload["far"], [4, 5, 6])
        self.assertEqual(payload["brightness"], 42)
        self.assertAlmostEqual(payload["active_ratio"], 0.25)
        self.assertIs(payload["screen_dark"], False)
        self.assertIs(payload["display_on"], True)
        self.assertIs(payload["lights_on"], True)
        self.assertIs(payload["rgb_on"], True)

    def test_the_payload_carries_every_field_the_extension_needs(self) -> None:
        """Guards the wire format: a dropped field is a silent behaviour change."""

        listener = _Listener(self.path)
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)
        publisher.publish((0, 0, 0), (0, 0, 0), **FRAME)

        self.assertEqual(
            set(listener.receive()),
            {
                "v",
                "near",
                "far",
                "brightness",
                "active_ratio",
                "screen_dark",
                "display_on",
                "lights_on",
                "rgb_on",
            },
        )

    def test_no_listener_is_not_an_error(self) -> None:
        """The normal state whenever the extension is not running."""

        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)
        for _ in range(5):
            self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))

    def test_a_socket_that_disappears_mid_run_is_not_an_error(self) -> None:
        listener = _Listener(self.path)
        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)

        self.assertTrue(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        listener.close()
        os.unlink(self.path)

        self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))

    def test_a_directory_where_the_socket_should_be_is_not_an_error(self) -> None:
        directory = Path(self.tmp.name) / "adir"
        directory.mkdir()
        publisher = AmbiencePublisher(directory)
        self.addCleanup(publisher.close)
        self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))

    def test_a_full_receive_buffer_drops_the_frame_rather_than_blocking(self) -> None:
        """A slow reader must cost one frame, not stall the light loop."""

        listener = _Listener(self.path)
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)

        # Never read; eventually the kernel buffer fills and sendto would block.
        results = [publisher.publish((1, 2, 3), (4, 5, 6), **FRAME) for _ in range(4000)]
        self.assertIn(True, results, "some frames should have gone out")
        # Whatever happened, nothing raised and we got here.

    def test_recovery_is_reported(self) -> None:
        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)
        self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        self.assertTrue(publisher._warned, "first failure latches the warning")

        listener = _Listener(self.path)
        self.addCleanup(listener.close)
        self.assertTrue(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        self.assertFalse(publisher._warned, "a successful send clears the latch")

    def test_close_is_idempotent(self) -> None:
        publisher = AmbiencePublisher(self.path)
        publisher.close()
        publisher.close()

    def test_publish_after_close_does_not_raise(self) -> None:
        """It re-opens rather than failing permanently, which is self-healing."""

        publisher = AmbiencePublisher(self.path)
        self.addCleanup(publisher.close)
        publisher.close()
        self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))


class NeverRaisesTest(unittest.TestCase):
    """Whatever the socket layer throws, publish returns."""

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_arbitrary_oserror_is_swallowed(self) -> None:
        publisher = AmbiencePublisher(Path("/nonexistent/dir/rgb.sock"))

        class Exploding:
            def sendto(self, *_args):
                raise OSError(99, "cannot assign requested address")

            def close(self):
                pass

        publisher._socket = Exploding()
        self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))

    def test_socket_creation_failure_is_swallowed(self) -> None:
        import matterlights.rgb_publish as module

        original = module.socket.socket
        module.socket.socket = lambda *a, **k: (_ for _ in ()).throw(OSError("no fds"))
        try:
            publisher = AmbiencePublisher(Path("/tmp/whatever.sock"))
            self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        finally:
            module.socket.socket = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
