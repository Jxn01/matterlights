"""Publish each tick's ambience colours for the optional local RGB extension.

MatterLights already captures the screen and renders an ambience palette several
times a second. A second program that wants the same colours -- here
ambience-rgb, driving motherboard, RAM, AIO and soundbar lighting on the same
machine -- should not capture the screen again: that doubles the capture load
and lets the two disagree during a scene change. So this publishes, and the
extension subscribes.

Two transports carry one payload: a unix datagram socket on Linux, and UDP on
the loopback on Windows, which has no unix datagram sockets at all. Both are
connectionless -- there is no connection to lose and no reader to block on.
``RGB_PUBLISH_SOCKET`` picks one; see :mod:`matterlights.rgb_target`.

⚠️ **This must never be able to break the sync loop.** The lights are the
product; the extension is a bystander. Every failure mode -- no listener, a
socket that was deleted, a full buffer, a path that is suddenly a directory, a
platform with no unix sockets -- is swallowed and logged once.

Disabled by default. With ``RGB_EXTENSION_ENABLED`` false this module is never
imported by the sync loop at all.
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

from matterlights.rgb_target import EXAMPLE, UdpTarget, parse_rgb_target

LOGGER = logging.getLogger("matterlights.rgb_publish")

# Bumped when the payload's meaning changes, so a mismatched extension can say
# so plainly instead of misreading fields.
PAYLOAD_VERSION = 2


class AmbiencePublisher:
    """Fire-and-forget datagram publisher. Safe to call on every tick."""

    def __init__(self, target: Path | UdpTarget | str, logger: logging.Logger | None = None) -> None:
        self._target = parse_rgb_target(target)
        self._udp = isinstance(self._target, UdpTarget)
        self._address: object = (
            (self._target.host, self._target.port) if self._udp else str(self._target)
        )
        self._label = str(self._target)
        self._logger = logger or LOGGER
        self._socket: socket.socket | None = None
        self._warned = False
        self._sent = 0
        self._clean_sends = 0
        # How many clean sends in a row prove frames are arriving again. Over a
        # unix socket, one: with no listener the send itself fails. Over UDP a
        # send to a closed port succeeds -- on Windows the failure surfaces on
        # the NEXT send -- so a single clean send proves nothing.
        self._proof = 2 if self._udp else 1

    def _ensure_socket(self) -> socket.socket | None:
        if self._socket is not None:
            return self._socket
        if self._udp:
            family = socket.AF_INET
        else:
            # 🚨 getattr, never socket.AF_UNIX: Windows CPython has no such
            # attribute, and the AttributeError used to escape the OSError
            # handler below and take the sync loop down at its first publish.
            family = getattr(socket, "AF_UNIX", None)
            if family is None:
                self._warn(f"Unix sockets do not exist on this platform; set RGB_PUBLISH_SOCKET={EXAMPLE}")
                return None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.setblocking(False)
        except OSError:
            self._warn("Could not create the RGB publish socket")
            return None
        self._socket = sock
        return sock

    def _drop_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _warn(self, message: str) -> None:
        """Log the first failure only.

        The loop runs several times a second; an unconditional warning would
        fill the journal with the same line thousands of times when the
        extension simply is not running, which is a perfectly normal state.
        """

        self._clean_sends = 0
        if not self._warned:
            self._warned = True
            self._logger.warning("%s (%s); RGB updates are not being delivered", message, self._label)

    def publish(
        self,
        near: tuple[int, int, int],
        far: tuple[int, int, int],
        *,
        palette: list[tuple[tuple[int, int, int], float]] | None = None,
        brightness: int,
        active_ratio: float,
        screen_dark: bool,
        display_on: bool,
        lights_on: bool,
        rgb_on: bool,
    ) -> bool:
        """Send one frame. Returns whether it went out; never raises.

        Over UDP "went out" is all it can mean: a datagram to a port nobody is
        listening on is sent just the same.
        """

        sock = self._ensure_socket()
        if sock is None:
            return False

        payload = {
            "v": PAYLOAD_VERSION,
            "near": list(near),
            "far": list(far),
            # The frame's whole weighted palette, richest first. Six bulbs can
            # only show one colour each; a 97-LED strip can show a gradient, and
            # this is the difference between the two.
            "palette": [
                {"rgb": list(colour), "weight": round(float(weight), 4)}
                for colour, weight in (palette or [])
            ],
            "brightness": int(brightness),
            "active_ratio": round(float(active_ratio), 4),
            "screen_dark": bool(screen_dark),
            "display_on": bool(display_on),
            "lights_on": bool(lights_on),
            "rgb_on": bool(rgb_on),
        }

        try:
            sock.sendto(json.dumps(payload).encode("utf-8"), self._address)
        except ConnectionResetError:
            # 🚨 Windows, UDP: an earlier send reached a closed port, the ICMP
            # port-unreachable came back, and Winsock reports it HERE -- on the
            # next send, as WSAECONNRESET. Microsoft says the socket is no longer
            # usable after that, so it is replaced on the next tick.
            self._drop_socket()
            self._warn("No RGB listener")
            return False
        except (FileNotFoundError, ConnectionRefusedError):
            # Nobody is listening. Normal and expected whenever the extension
            # is not running; not worth a warning beyond the first.
            self._warn("No RGB listener")
            return False
        except BlockingIOError:
            # The receiver is behind. Dropping this frame is correct: the next
            # one is a tick away and carries fresher colour anyway.
            return False
        except OSError as error:
            self._warn(f"RGB publish failed: {error}")
            return False

        self._sent += 1
        if self._warned:
            self._clean_sends += 1
            if self._clean_sends >= self._proof:
                # Recovered: say so once, so a log showing the warning does not
                # imply it is still broken.
                self._warned = False
                self._clean_sends = 0
                self._logger.info("RGB listener is receiving again (%s)", self._label)
        return True

    def close(self) -> None:
        self._drop_socket()
