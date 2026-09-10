"""Publish each tick's ambience colours for the optional local RGB extension.

MatterLights already captures the screen and renders an ambience palette several
times a second. A second program that wants the same colours -- here, a private
Linux-only project driving motherboard and AIO RGB -- should not capture the
screen again: that doubles the PipeWire load and lets the two disagree during a
scene change. So this publishes, and the extension subscribes.

⚠️ **This must never be able to break the sync loop.** The lights are the
product; the extension is a bystander. Every failure mode -- no listener, a
socket that was deleted, a full buffer, a path that is suddenly a directory --
is swallowed and logged once. A datagram socket is used precisely because it has
no connection to lose and no reader to block on: sending to a unix datagram
socket that nobody has bound fails immediately rather than waiting.

Disabled by default. With ``RGB_EXTENSION_ENABLED`` false this module is never
imported by the sync loop at all.
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

LOGGER = logging.getLogger("matterlights.rgb_publish")

# Bumped when the payload's meaning changes, so a mismatched extension can say
# so plainly instead of misreading fields.
PAYLOAD_VERSION = 2


class AmbiencePublisher:
    """Fire-and-forget datagram publisher. Safe to call on every tick."""

    def __init__(self, socket_path: Path, logger: logging.Logger | None = None) -> None:
        self._path = str(socket_path)
        self._logger = logger or LOGGER
        self._socket: socket.socket | None = None
        self._warned = False
        self._sent = 0

    def _ensure_socket(self) -> socket.socket | None:
        if self._socket is not None:
            return self._socket
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
            self._socket = sock
            return sock
        except OSError:
            self._warn("Could not create the RGB publish socket")
            return None

    def _warn(self, message: str) -> None:
        """Log the first failure only.

        The loop runs several times a second; an unconditional warning would
        fill the journal with the same line thousands of times when the
        extension simply is not running, which is a perfectly normal state.
        """

        if not self._warned:
            self._warned = True
            self._logger.warning("%s (%s); RGB updates are not being delivered", message, self._path)

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
        """Send one frame. Returns whether it went out; never raises."""

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
            sock.sendto(json.dumps(payload).encode("utf-8"), self._path)
        except (FileNotFoundError, ConnectionRefusedError):
            # Nobody is listening. Normal and expected whenever the extension
            # is not running; not worth a warning beyond the first.
            self._warn("No RGB listener")
            return False
        except BlockingIOError:
            # The receiver is behind. Dropping this frame is correct: the next
            # one is 200 ms away and carries fresher colour anyway.
            return False
        except OSError as error:
            self._warn(f"RGB publish failed: {error}")
            return False

        if self._warned:
            # Recovered: say so once, so a journal showing the warning does not
            # imply it is still broken.
            self._warned = False
            self._logger.info("RGB listener is receiving again (%s)", self._path)
        self._sent += 1
        return True

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
