"""Exclusive fullscreen on Windows: notice it, say what the capture delivered, re-create the duplication if it starves.

The owner's A/B (Warframe, 2026-10-25): **borderless fullscreen kept the lights in sync; exclusive fullscreen turned
them all black.** The log of that weekend could not say why. With the lamps switched off, the loop runs the RGB-only
path, which decided "screen dark" without a log line; and dxcam logs only a lost duplication
(``DXGI_ERROR_ACCESS_LOST``, which it recovers from by itself), never a duplication that simply stops delivering
(``AcquireNextFrame`` timing out, which dxcam reports as "no new frame" and the backend answers by reusing the last
frame). Nothing was logged during play, so the capture either delivered black frames or delivered none. This
module makes the next exclusive-fullscreen session tell the two apart:

* the shell's notification state (``SHQueryUserNotificationState``; 3 = ``QUNS_RUNNING_D3D_FULL_SCREEN``) is polled
  once a second, and every change is logged with what the capture delivered just before it: fresh and reused frame
  counts, the last frame's size and mean brightness;
* while it is exclusive, the same summary is logged every ``SUMMARY_SECONDS``;
* while it is exclusive and no fresh frame has arrived for ``STARVE_SECONDS``, the backend is asked to re-create its
  camera (at most ``MAX_RECREATES`` per exclusive episode, ``STARVE_SECONDS`` apart). Microsoft documents that a
  full-screen application normally invalidates the duplication with ``DXGI_ERROR_ACCESS_LOST``; this covers the
  silent case the weekend log points at, and logs that it did.

Pure: the clock, the shell state and the frames come in as arguments, so every rule runs in the Linux test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Callable

LOGGER = logging.getLogger("matterlights.capture.fullscreen")

QUNS_D3D_FULLSCREEN = 3
QUNS_NAMES = {
    1: "not present (locked, screensaver or fast user switching)",
    2: "busy (a fullscreen app, not exclusive D3D)",
    3: "exclusive D3D fullscreen",
    4: "presentation mode",
    5: "normal desktop",
    6: "quiet time",
    7: "a fullscreen Store app",
}
POLL_SECONDS = 1.0
STARVE_SECONDS = 5.0
SUMMARY_SECONDS = 30.0
MAX_RECREATES = 3


NOT_POLLED = -1   # before the first poll, so the first answer (even a failed query) is always logged


def state_name(state: int | None) -> str:
    if state == NOT_POLLED:
        return "not polled yet"
    if state is None:
        return "unknown (the shell query failed)"
    return f"{state} = {QUNS_NAMES.get(state, 'undocumented')}"


def mean_brightness(raw: bytes, width: int, height: int, samples: int = 4096) -> float | None:
    """Mean of (B+G+R)/3 over up to ``samples`` evenly spaced BGRx pixels; 0 is black, 255 white."""
    pixels = width * height
    if pixels <= 0 or len(raw) < pixels * 4:
        return None
    step = max(1, pixels // samples)
    total = count = 0
    for p in range(0, pixels, step):
        i = p * 4
        total += raw[i] + raw[i + 1] + raw[i + 2]
        count += 1
    return total / (3 * count) if count else None


@dataclass
class _Last:
    raw: bytes = b""
    width: int = 0
    height: int = 0
    backend: str = ""


@dataclass
class FullscreenWatch:
    log: Callable[..., None] = field(default=LOGGER.info)
    warn: Callable[..., None] = field(default=LOGGER.warning)
    state: int | None = NOT_POLLED
    _polled_at: float = float("-inf")
    _fresh: int = 0
    _reused: int = 0
    _last_fresh_at: float | None = None
    _next_summary: float = float("inf")
    _recreates: int = 0
    _last_recreate_at: float = float("-inf")
    _last: _Last = field(default_factory=_Last)

    @property
    def exclusive(self) -> bool:
        return self.state == QUNS_D3D_FULLSCREEN

    def should_poll(self, now: float) -> bool:
        return now - self._polled_at >= POLL_SECONDS

    def _delivery(self, now: float) -> str:
        last = self._last
        bright = mean_brightness(last.raw, last.width, last.height)
        age = "never" if self._last_fresh_at is None else f"{now - self._last_fresh_at:.1f} s ago"
        return (f"frames since the last report: {self._fresh} fresh, {self._reused} reused; last fresh frame {age}; "
                f"last frame {last.width}x{last.height} via {last.backend or '-'}, mean brightness "
                f"{'-' if bright is None else f'{bright:.1f}/255'}")

    def on_state(self, now: float, state: int | None) -> None:
        self._polled_at = now
        if state == self.state:
            return
        before = self.state
        self.state = state
        self.log("Fullscreen state %s -> %s; %s", state_name(before), state_name(state), self._delivery(now))
        self._fresh = self._reused = 0
        if self.exclusive:
            self._recreates = 0
            self._last_recreate_at = float("-inf")
            self._next_summary = now + SUMMARY_SECONDS
        else:
            self._next_summary = float("inf")

    def on_frame(self, now: float, raw: bytes, width: int, height: int, *, fresh: bool, backend: str) -> bool:
        """Note one grabbed frame. True = the backend should re-create its camera now."""
        if fresh:
            self._fresh += 1
            self._last_fresh_at = now
        else:
            self._reused += 1
        self._last = _Last(raw, width, height, backend)
        if not self.exclusive:
            return False
        if now >= self._next_summary:
            self.log("Exclusive fullscreen: %s", self._delivery(now))
            self._fresh = self._reused = 0
            self._next_summary = now + SUMMARY_SECONDS
        starving = self._last_fresh_at is None or now - self._last_fresh_at >= STARVE_SECONDS
        if (backend == "dxgi" and starving and self._recreates < MAX_RECREATES
                and now - self._last_recreate_at >= STARVE_SECONDS):
            self._recreates += 1
            self._last_recreate_at = now
            self.warn("Exclusive fullscreen and no fresh Desktop Duplication frame for %.0f s: re-creating the "
                      "camera (%d of %d this episode)", STARVE_SECONDS, self._recreates, MAX_RECREATES)
            return True
        return False
