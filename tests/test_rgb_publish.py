"""The RGB publisher must never be able to break the sync loop.

The lights are the product; the RGB extension is a bystander. Every test here
exists to prove one thing: whatever goes wrong on the socket, ``publish``
returns and the caller carries on. A regression that lets an exception escape
would take the bulbs down whenever the extension was stopped -- which is the
normal state on every machine except one.

Two transports carry the same payload. A unix datagram socket on Linux; UDP on
the loopback where there is no such thing -- Windows, whose ``AF_UNIX`` is
``SOCK_STREAM`` only. The unix tests skip on Windows; everything else runs on
both, so the Windows path is exercised from Linux on every run.
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
from unittest import mock

import matterlights.rgb_publish as rgb_publish
from matterlights.rgb_publish import PAYLOAD_VERSION, AmbiencePublisher
from matterlights.rgb_target import UdpTarget

HAS_UNIX_DATAGRAMS = hasattr(socket, "AF_UNIX") and sys.platform != "win32"

FRAME = dict(
    brightness=42,
    active_ratio=0.25,
    screen_dark=False,
    display_on=True,
    lights_on=True,
    rgb_on=True,
)


class _Listener:
    """A bound unix datagram socket standing in for the extension."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(2.0)

    def receive(self) -> dict:
        return json.loads(self.sock.recv(65535).decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


class _UdpListener:
    """The extension as it listens on Windows: UDP on the loopback."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2.0)
        self.port = self.sock.getsockname()[1]
        self.url = f"udp://127.0.0.1:{self.port}"

    def receive(self) -> dict:
        return json.loads(self.sock.recv(65535).decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


def _closed_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@unittest.skipUnless(HAS_UNIX_DATAGRAMS, "unix datagram sockets")
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
                "palette",
                "brightness",
                "active_ratio",
                "screen_dark",
                "display_on",
                "lights_on",
                "rgb_on",
            },
        )

    def test_a_string_path_is_a_unix_socket_too(self) -> None:
        # RGB_PUBLISH_SOCKET arrives as text; a plain path must still mean unix.
        listener = _Listener(self.path)
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(str(self.path))
        self.addCleanup(publisher.close)
        self.assertTrue(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        self.assertEqual(listener.receive()["near"], [1, 2, 3])

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
        # Over a unix socket one clean send is proof of a listener: with none,
        # the send itself fails.
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


class UdpPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_udp_listener_receives_the_frame(self) -> None:
        listener = _UdpListener()
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(listener.url)
        self.addCleanup(publisher.close)

        self.assertTrue(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        payload = listener.receive()
        self.assertEqual(payload["v"], PAYLOAD_VERSION)
        self.assertEqual(payload["near"], [1, 2, 3])
        self.assertEqual(payload["far"], [4, 5, 6])

    def test_a_target_object_works_as_well_as_a_url(self) -> None:
        listener = _UdpListener()
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(UdpTarget("127.0.0.1", listener.port))
        self.addCleanup(publisher.close)
        self.assertTrue(publisher.publish((7, 8, 9), (0, 0, 0), **FRAME))
        self.assertEqual(listener.receive()["near"], [7, 8, 9])

    def test_no_listener_is_not_an_error(self) -> None:
        publisher = AmbiencePublisher(f"udp://127.0.0.1:{_closed_udp_port()}")
        self.addCleanup(publisher.close)
        for _ in range(5):
            publisher.publish((1, 2, 3), (4, 5, 6), **FRAME)

    @unittest.skipUnless(HAS_UNIX_DATAGRAMS, "unix datagram sockets")
    def test_both_transports_carry_the_identical_payload(self) -> None:
        """One wire format. The extension must not be able to tell them apart."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rgb.sock"
            unix_listener = _Listener(path)
            self.addCleanup(unix_listener.close)
            udp_listener = _UdpListener()
            self.addCleanup(udp_listener.close)
            frame = dict(FRAME, palette=[((255, 0, 0), 0.6), ((0, 0, 255), 0.4)])
            for target in (path, udp_listener.url):
                publisher = AmbiencePublisher(target)
                publisher.publish((1, 2, 3), (4, 5, 6), **frame)
                publisher.close()
            self.assertEqual(unix_listener.receive(), udp_listener.receive())


class _ScriptedSocket:
    """A socket whose sends succeed or raise, in the order given."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.closed = False

    def setblocking(self, _flag) -> None:
        pass

    def sendto(self, data, _address):
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return len(data)

    def close(self) -> None:
        self.closed = True


def _reset() -> ConnectionResetError:
    return ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")


class WindowsConnectionResetTest(unittest.TestCase):
    """🚨 Windows reports a UDP send to a closed port on the NEXT send.

    The closed port answers with an ICMP port-unreachable, and Winsock hands that
    to the socket's following ``sendto`` as ``WSAECONNRESET`` --
    ``ConnectionResetError``. Microsoft's documentation says the socket is then
    no longer usable. So the publisher drops it and makes a new one next tick,
    never raises, and does not let the resulting rhythm -- sent, reset, sent,
    reset, while the extension is stopped -- flap the log between a warning and
    a recovery twice a second.
    """

    TARGET = "udp://127.0.0.1:9"

    def _publish(self, publisher: AmbiencePublisher) -> bool:
        return publisher.publish((1, 2, 3), (4, 5, 6), **FRAME)

    def test_a_reset_drops_the_socket_and_the_next_publish_makes_a_new_one(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        publisher = AmbiencePublisher(self.TARGET)
        self.addCleanup(publisher.close)
        dead = _ScriptedSocket([_reset()])
        publisher._socket = dead
        self.assertFalse(self._publish(publisher))
        self.assertTrue(dead.closed)
        fresh = _ScriptedSocket([])
        with mock.patch.object(rgb_publish.socket, "socket", lambda *_a, **_k: fresh):
            self.assertTrue(self._publish(publisher))
        self.assertIs(publisher._socket, fresh)

    def test_the_windows_rhythm_warns_once_and_never_claims_recovery(self) -> None:
        made: list[_ScriptedSocket] = []

        def factory(*_args, **_kwargs):
            made.append(_ScriptedSocket([None, _reset()]))
            return made[-1]

        publisher = AmbiencePublisher(self.TARGET)
        self.addCleanup(publisher.close)
        with mock.patch.object(rgb_publish.socket, "socket", factory), self.assertLogs(
            "matterlights.rgb_publish", level="INFO"
        ) as logs:
            for _ in range(20):
                self._publish(publisher)
        warnings = [record for record in logs.records if record.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, [record.getMessage() for record in logs.records])
        self.assertFalse(any("receiving again" in record.getMessage() for record in logs.records))
        self.assertGreater(len(made), 5, "each reset must cost the socket")

    def test_two_clean_sends_in_a_row_are_proof_of_a_listener(self) -> None:
        sockets = iter([_ScriptedSocket([_reset()]), _ScriptedSocket([])])
        publisher = AmbiencePublisher(self.TARGET)
        self.addCleanup(publisher.close)
        with mock.patch.object(rgb_publish.socket, "socket", lambda *_a, **_k: next(sockets)), self.assertLogs(
            "matterlights.rgb_publish", level="INFO"
        ) as logs:
            self.assertFalse(self._publish(publisher))
            self._publish(publisher)
            self.assertTrue(publisher._warned, "one clean UDP send proves nothing")
            self._publish(publisher)
        self.assertFalse(publisher._warned)
        recoveries = [r for r in logs.records if "receiving again" in r.getMessage()]
        self.assertEqual(len(recoveries), 1)


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
        for target in (Path("/tmp/whatever.sock"), "udp://127.0.0.1:9"):
            with self.subTest(target=str(target)):
                with mock.patch.object(
                    rgb_publish.socket, "socket", mock.Mock(side_effect=OSError("no fds"))
                ):
                    publisher = AmbiencePublisher(target)
                    self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))

    def test_no_unix_sockets_at_all_is_a_warning_not_a_crash(self) -> None:
        """🚨 ``socket.AF_UNIX`` does not exist on Windows CPython.

        Looking it up raised ``AttributeError``, which the ``OSError`` handler
        around it did not catch -- so a unix path in ``RGB_PUBLISH_SOCKET`` on
        Windows took the whole sync loop down at its first publish. Latent only
        because the extension defaults to off.
        """

        logging.disable(logging.NOTSET)
        with mock.patch.dict(socket.__dict__):
            socket.__dict__.pop("AF_UNIX", None)
            with self.assertLogs("matterlights.rgb_publish", level="WARNING") as logs:
                publisher = AmbiencePublisher(Path("/tmp/rgb.sock"))
                self.assertFalse(publisher.publish((1, 2, 3), (4, 5, 6), **FRAME))
        self.assertIn("udp://", "\n".join(logs.output), "say what to use instead")


class PaletteTest(unittest.TestCase):
    """The palette is the whole point of payload v2.

    Six bulbs can show one colour each, so the engine's four-colour palette was
    collapsed to a single near/far pair before it ever reached the socket. A
    97-LED strip can render the whole thing as a gradient, which is the
    difference between "the case is blue" and an ambience with depth.

    Over UDP, so this runs on both platforms.
    """

    def _round_trip(self, **kwargs):
        listener = _UdpListener()
        self.addCleanup(listener.close)
        publisher = AmbiencePublisher(listener.url)
        self.addCleanup(publisher.close)
        publisher.publish((1, 2, 3), (4, 5, 6), **{**FRAME, **kwargs})
        return listener.receive()

    def test_it_carries_colours_and_weights(self) -> None:
        payload = self._round_trip(
            palette=[((255, 0, 0), 0.5), ((0, 0, 255), 0.3), ((0, 255, 0), 0.2)]
        )
        self.assertEqual([entry["rgb"] for entry in payload["palette"]],
                         [[255, 0, 0], [0, 0, 255], [0, 255, 0]])
        self.assertEqual([entry["weight"] for entry in payload["palette"]], [0.5, 0.3, 0.2])

    def test_order_is_preserved(self) -> None:
        """Richest first: a consumer apportioning LED spans relies on it."""

        payload = self._round_trip(
            palette=[((9, 9, 9), 0.9), ((1, 1, 1), 0.1)]
        )
        self.assertEqual(payload["palette"][0]["weight"], 0.9)

    def test_no_palette_is_an_empty_list_not_a_missing_key(self) -> None:
        """A consumer must never have to distinguish absent from empty."""

        payload = self._round_trip(palette=None)
        self.assertEqual(payload["palette"], [])

    def test_the_version_says_v2(self) -> None:
        self.assertEqual(self._round_trip()["v"], PAYLOAD_VERSION)
        self.assertEqual(PAYLOAD_VERSION, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
