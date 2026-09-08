"""Screen capture, and the only place in the package that knows which OS it is on.

``screen.py`` used to do both jobs -- grabbing frames and turning them into
colours. Those are not the same concern: the colour engine is arithmetic over a
byte buffer and runs anywhere, while grabbing a frame is the single most
platform-bound thing the project does. Keeping them in one module is what made
``from ctypes import windll`` a package-wide outage on Linux.

So the split is: a backend hands over **raw BGRx bytes plus dimensions**, and
everything downstream is platform-free. BGRx is not an arbitrary choice -- it is
already what ``_sample_zone`` indexes (``blue=+0, green=+1, red=+2``), it is what
DXGI Desktop Duplication produces on Windows, and it is what PipeWire negotiates
on Linux. Both platforms converge on it for free, which is why no colour code
had to change for the Linux port.

Backends are cached per process: a Windows ``MSS`` session and a Linux PipeWire
stream are both expensive to build and meant to be long-lived.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sys
import threading
from typing import Protocol

LOGGER = logging.getLogger("matterlights.capture")


@dataclass(frozen=True, slots=True)
class Monitor:
    """One screen, in PHYSICAL pixels, positioned on the virtual desktop.

    ``index`` is 1-based to match what users type in ``SCREEN_CAPTURE_TARGET``;
    index 0 is reserved for the virtual bounding box covering every screen (the
    ``all`` target), mirroring the convention ``mss`` uses.

    Physical pixels matter and both platforms have a way to get this wrong.
    Windows' ``GetSystemMetrics`` reports DPI-virtualised pixels (3072x1728 for a
    4K panel at 125%); Linux's XWayland reports DP-2 as 7680x4320 for the same
    3840x2160 panel. Neither number is a real monitor rectangle. The backends are
    responsible for reporting the true one.
    """

    index: int
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    name: str = ""

    def as_dict(self) -> dict[str, int | bool | str]:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
            "name": self.name,
        }


class CaptureBackend(Protocol):
    """One frame source. Implementations live in ``windows.py`` / ``linux.py``."""

    def grab(self, capture_target: str) -> tuple[bytes, int, int]:
        """Return one frame as ``(raw_bgrx, width, height)``.

        Implementations must return the PREVIOUS frame rather than raise or
        report black when no new frame is available. Both backends are
        damage-driven -- dxcam's ``grab()`` returns ``None`` and PipeWire simply
        sends nothing -- so "no new frame" is the normal state of a static
        desktop, not an error. Reporting black there would blink the lights off
        every time the user stopped moving the mouse.
        """

    def list_monitors(self) -> list[Monitor]: ...

    def grab_png(self, capture_target: str, max_width: int | None = None) -> tuple[bytes, int, int]:
        """Return one frame as PNG bytes plus its dimensions.

        ``max_width`` downscales by pixel striding before encoding, for previews.
        """

    def close(self) -> None: ...


_BACKEND: CaptureBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> CaptureBackend:
    """Return the process-wide capture backend, creating it on first use."""

    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            _BACKEND = _create_backend()
        return _BACKEND


def reset_backend() -> None:
    """Drop the cached backend, closing it first.

    Used by tests, and by the Linux backend when the compositor invalidates a
    session (a monitor was unplugged, the stream closed) and it has to be rebuilt.
    """

    global _BACKEND
    with _BACKEND_LOCK:
        backend, _BACKEND = _BACKEND, None
    if backend is not None:
        try:
            backend.close()
        except Exception:  # noqa: BLE001 - closing must never propagate
            LOGGER.debug("Capture backend failed to close cleanly", exc_info=True)


def _create_backend() -> CaptureBackend:
    if sys.platform == "win32":
        from matterlights.capture.windows import WindowsCaptureBackend

        return WindowsCaptureBackend()

    from matterlights.capture.linux import LinuxCaptureBackend

    return LinuxCaptureBackend()


# ---------------------------------------------------------------------------
# Shared pixel helpers. These are pure byte arithmetic, identical on every
# platform, and deliberately dependency-free -- no Pillow, no numpy.
# ---------------------------------------------------------------------------


def bgrx_to_rgb(raw: bytes, width: int, height: int, stride: int | None = None) -> bytes:
    """Convert a BGRx buffer to packed RGB.

    Slice assignment rather than a Python loop: this runs over 33 MB for a 4K
    frame, and the loop version takes seconds where this takes milliseconds.

    ``stride`` is the real distance in bytes between the start of one row and the
    next. It is NOT always ``width * 4`` -- GStreamer pads rows to an alignment
    boundary -- and using the wrong one shears the image diagonally, which looks
    like a corrupt capture rather than an arithmetic mistake.
    """

    packed_stride = width * 4
    if stride is not None and stride != packed_stride:
        rows = [raw[y * stride : y * stride + packed_stride] for y in range(height)]
        raw = b"".join(rows)

    rgb = bytearray(width * height * 3)
    rgb[0::3] = raw[2::4]
    rgb[1::3] = raw[1::4]
    rgb[2::3] = raw[0::4]
    return bytes(rgb)


def rgb_to_png(rgb: bytes, size: tuple[int, int]) -> bytes:
    """Encode packed RGB as a PNG.

    Uses ``mss.tools.to_png``, which is pure-Python zlib framing with no X11 or
    Windows dependency of its own -- so ``mss`` stays a cross-platform
    dependency purely as a PNG encoder, long after it stopped being the capture
    backend on Linux.
    """

    from mss import tools

    return tools.to_png(rgb, size)


def stride_rgb(rgb: bytes, width: int, height: int, step: int) -> tuple[bytes, int, int]:
    """Nearest-neighbour downscale of packed RGB by taking every ``step`` pixel.

    A 4K frame encodes to roughly 8 MB of PNG and the whole virtual desktop to
    ~40 MB, which is far too heavy to hand a browser for a preview thumbnail.
    """

    if step <= 1:
        return rgb, width, height

    row_stride = width * 3
    columns = range(0, width, step)
    rows = bytearray()
    for y_pos in range(0, height, step):
        row_start = y_pos * row_stride
        row = rgb[row_start : row_start + row_stride]
        rows += b"".join(row[x_pos * 3 : x_pos * 3 + 3] for x_pos in columns)

    return bytes(rows), len(columns), len(range(0, height, step))
