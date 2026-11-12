"""Frames from an LG TV, for a matterlights that runs on a machine beside it.

The rooted TV's companion (repo tv-hub, ``tv/jxn-hub-agent.py``) sends one UDP
packet per frame: ``b"JXNF"``, then ``!IHHB`` (sequence, width, height, flag),
then packed RGB. The flag says what the TV is showing:

* ``FRAMES``: a picture, sampled like any screen.
* ``PROTECTED``: the TV will not hand the picture over (Netflix) and a warm glow
  stands in for it. A steady warm frame.
* ``IDLE``: nothing to follow -- its own Home screen, the screen saver, standby,
  the lights not being the TV's. Black.

Black, not ``CaptureUnavailable``, for idle and for silence. "Skip this tick"
would freeze the bulbs on the last colour for as long as the TV stays off, which
is every night. A black frame takes the road a dark screen already takes through
the sync loop: the dark rule turns the lights off once, and nothing else is sent.
Silence counts as off because a TV that is off sends nothing at all, so it cannot
say goodbye.

Only the configured sender is believed. The firewall in front of this port says
the same; this keeps a mistake there from turning into coloured bulbs.
"""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
import time
from typing import Callable

from matterlights.capture import Monitor, TvSource, bgrx_to_rgb, rgb_to_bgrx, rgb_to_png

LOGGER = logging.getLogger("matterlights.capture.tv")

MAGIC = b"JXNF"
HEADER = struct.Struct("!IHHB")
FLAG_IDLE, FLAG_FRAMES, FLAG_PROTECTED = 0, 1, 2

# The companion says "idle" every 2 s, so this is three lost packets. A shorter
# wait would blink the lights off over a stalled capture.
STALE_SECONDS = 6.0

BLANK_SIZE = (64, 36)
WARM_RGB = (255, 170, 95)


def parse_packet(data: bytes) -> tuple[int, int, int, bytes] | None:
    """``(flag, width, height, rgb)``, or None for anything that is not one of ours."""

    if len(data) < len(MAGIC) + HEADER.size or not data.startswith(MAGIC):
        return None
    _, width, height, flag = HEADER.unpack_from(data, len(MAGIC))
    rgb = data[len(MAGIC) + HEADER.size :]
    if flag == FLAG_FRAMES:
        if width == 0 or height == 0 or len(rgb) != width * height * 3:
            return None
    elif flag not in (FLAG_IDLE, FLAG_PROTECTED):
        return None
    return flag, width, height, rgb


def solid_bgrx(rgb: tuple[int, int, int], width: int, height: int) -> bytes:
    red, green, blue = rgb
    return bytes((blue, green, red, 0)) * (width * height)


class TvCaptureBackend:
    """A ``CaptureBackend`` fed by the network instead of by a compositor."""

    def __init__(self, source: TvSource, clock: Callable[[], float] = time.monotonic) -> None:
        self._sender = source.sender
        self._clock = clock
        width, height = BLANK_SIZE
        self._black = (solid_bgrx((0, 0, 0), width, height), width, height)
        self._warm = (solid_bgrx(WARM_RGB, width, height), width, height)
        self._lock = threading.Lock()
        self._frame = self._black
        self._heard_at = -math.inf
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((source.bind, source.port))
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._listen, name="matterlights-tv-frames", daemon=True)
        self._thread.start()
        LOGGER.info("Listening for TV frames from %s on %s:%s", source.sender, source.bind, source.port)

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                LOGGER.exception("TV frame socket failed; retrying")
                self._stop.wait(1.0)
                continue
            self.receive(data, address[0])

    def receive(self, data: bytes, sender: str) -> None:
        """Take one datagram. Anything from elsewhere, or malformed, changes nothing."""

        if sender != self._sender:
            return
        packet = parse_packet(data)
        if packet is None:
            return
        flag, width, height, rgb = packet
        if flag == FLAG_FRAMES:
            frame = (rgb_to_bgrx(rgb), width, height)
        elif flag == FLAG_PROTECTED:
            frame = self._warm
        else:
            frame = self._black
        with self._lock:
            self._frame = frame
            self._heard_at = self._clock()

    def grab(self, capture_target: str) -> tuple[bytes, int, int]:
        with self._lock:
            if self._clock() - self._heard_at > STALE_SECONDS:
                return self._black
            return self._frame

    def list_monitors(self) -> list[Monitor]:
        _, width, height = self.grab("primary")
        return [Monitor(index=1, left=0, top=0, width=width, height=height, is_primary=True, name="LG TV")]

    def grab_png(self, capture_target: str, max_width: int | None = None) -> tuple[bytes, int, int]:
        raw, width, height = self.grab(capture_target)
        return rgb_to_png(bgrx_to_rgb(raw, width, height), (width, height)), width, height

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2.0)
