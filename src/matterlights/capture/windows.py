"""Windows capture: DXGI Desktop Duplication (dxcam), falling back to mss/GDI.

WHY THIS EXISTS. mss captures by BitBlt-ing from the screen DC. Sustained
full-screen GDI readback on this rig (RTX 5090, five displays, 4K HDR primary)
makes the compositor mis-draw the mouse cursor at the capture origin -- it
visibly snaps to the top-left of the captured monitor several times a second,
and the text caret in other applications flickers with it. Measured 2026-09-08:
with MatterLights stopped, a bare capture loop reproduced it on its own after
roughly two minutes of sustained grabbing, and it stopped the moment capture
stopped. GetCursorPos never moves during this, so nothing is moving the pointer
-- it is purely a compositing artifact of the GDI screen readback.

Desktop Duplication avoids the problem class entirely: it receives frames from
DXGI instead of reading back the screen DC, and is substantially faster.

Two dxcam behaviours callers must not trip over:
  * grab() returns None when no NEW frame has arrived since the last call. On a
    static screen that is normal, not an error, so the last good frame is kept.
  * A camera is bound to ONE output, so the "all monitors" target cannot use it.
Anything unsupported falls back to mss, so behaviour never regresses.

This module was lifted out of ``screen.py`` unchanged when the Linux port split
capture from colour sampling. Its logic is the same code that has been running
on Windows since 2026-09-08; the only additions are the ``CaptureBackend``
wrapper and monitor enumeration moving behind the shared ``Monitor`` type.
"""

from __future__ import annotations

from ctypes import windll
import logging
import re
import threading

from mss import MSS

from matterlights.capture import Monitor, bgrx_to_rgb, rgb_to_png, stride_rgb

LOGGER = logging.getLogger(__name__)

_DXCAM_LOCK = threading.Lock()
_DXCAM_CAMERAS: dict[tuple[int, int], object] = {}
_DXCAM_LAST_FRAME: dict[tuple[int, int], tuple[bytes, int, int]] = {}
_DXCAM_OUTPUTS: list[dict[str, int | bool]] | None = None
_DXCAM_UNAVAILABLE = False

_OUTPUT_INFO_RE = re.compile(
    r"Device\[(\d+)\]\s*Output\[(\d+)\]:\s*Res:\((\d+),\s*(\d+)\)\s*Rot:(\d+)\s*Primary:(True|False)"
)


def _dxcam_outputs() -> list[dict[str, int | bool]]:
    """Parse dxcam's output table into something matchable against mss monitors.

    dxcam reports resolution/rotation/primary but not desktop position, so the
    match below is on size plus the primary flag.
    """

    global _DXCAM_OUTPUTS, _DXCAM_UNAVAILABLE
    if _DXCAM_OUTPUTS is not None:
        return _DXCAM_OUTPUTS
    try:
        import dxcam  # imported lazily so mss-only environments still work

        outputs: list[dict[str, int | bool]] = []
        for match in _OUTPUT_INFO_RE.finditer(dxcam.output_info()):
            device_idx, output_idx, width, height, _rotation, primary = match.groups()
            outputs.append(
                {
                    "device_idx": int(device_idx),
                    "output_idx": int(output_idx),
                    "width": int(width),
                    "height": int(height),
                    "primary": primary == "True",
                }
            )
        _DXCAM_OUTPUTS = outputs
        LOGGER.info("Desktop Duplication available: %d output(s)", len(outputs))
    except Exception as error:  # noqa: BLE001 - any failure means "use mss"
        _DXCAM_UNAVAILABLE = True
        _DXCAM_OUTPUTS = []
        LOGGER.warning("Desktop Duplication unavailable (%s); using GDI capture", error)
    return _DXCAM_OUTPUTS


def _dxcam_output_for_region(region: dict) -> tuple[int, int] | None:
    """Find the single dxcam output that exactly covers ``region``.

    Returns None when the region spans several screens (the ``all`` target) or
    when two outputs share a resolution and cannot be told apart -- both cases
    fall back to mss rather than risk capturing the wrong screen.
    """

    outputs = _dxcam_outputs()
    if not outputs:
        return None

    width = int(region.get("width", 0))
    height = int(region.get("height", 0))
    candidates = [o for o in outputs if o["width"] == width and o["height"] == height]
    if not candidates:
        return None

    if len(candidates) > 1:
        primary_candidates = [o for o in candidates if o["primary"]]
        if region.get("is_primary") and len(primary_candidates) == 1:
            candidates = primary_candidates
        else:
            # Two identical screens (the pair of portrait 4K panels here) cannot be
            # distinguished from resolution alone, and dxcam exposes no position.
            return None

    chosen = candidates[0]
    return int(chosen["device_idx"]), int(chosen["output_idx"])


def _dxcam_capture(region: dict) -> tuple[bytes, int, int] | None:
    """Grab ``region`` via Desktop Duplication, or None if that is not possible."""

    if _DXCAM_UNAVAILABLE:
        return None
    key = _dxcam_output_for_region(region)
    if key is None:
        return None

    try:
        import dxcam

        with _DXCAM_LOCK:
            camera = _DXCAM_CAMERAS.get(key)
            if camera is None:
                device_idx, output_idx = key
                # BGRA matches the byte order the zone sampler expects, so the
                # frame can be handed on without a colour conversion.
                camera = dxcam.create(
                    device_idx=device_idx, output_idx=output_idx, output_color="BGRA"
                )
                if camera is None:
                    return None
                _DXCAM_CAMERAS[key] = camera

            frame = camera.grab()
            if frame is None:
                # No new frame since the last call: the screen has not changed.
                # Reuse the previous one rather than reporting a black screen,
                # which would make the lights blink off on a static desktop.
                return _DXCAM_LAST_FRAME.get(key)

            height, width = frame.shape[0], frame.shape[1]
            raw = frame.tobytes()
            _DXCAM_LAST_FRAME[key] = (raw, width, height)
            return raw, width, height
    except Exception as error:  # noqa: BLE001
        LOGGER.warning("Desktop Duplication capture failed (%s); using GDI capture", error)
        with _DXCAM_LOCK:
            _DXCAM_CAMERAS.pop(key, None)
        return None


def _primary_monitor_region(sct: MSS | None = None) -> dict[str, int]:
    """Describe the primary screen, in PHYSICAL pixels.

    ``GetSystemMetrics(SM_CXSCREEN)`` returns DPI-VIRTUALISED pixels in a
    DPI-unaware process: on this machine's 3840x2160 panel at 125% scaling it
    reports 3072x1728. That still captures the whole screen (scaled), so zone
    sampling was unaffected -- but it never equals a real monitor rectangle,
    which stopped Desktop Duplication from recognising the region and silently
    forced the slow GDI path. mss reports physical pixels, so prefer its monitor
    table and keep GetSystemMetrics purely as a fallback.
    """

    if sct is not None:
        for monitor in sct.monitors[1:]:
            if monitor.get("is_primary"):
                return dict(monitor)
        if len(sct.monitors) > 1:
            return dict(sct.monitors[1])
    return {
        "left": 0,
        "top": 0,
        "width": int(windll.user32.GetSystemMetrics(0)),
        "height": int(windll.user32.GetSystemMetrics(1)),
    }


def _capture_region(sct: MSS, capture_target: str) -> dict[str, int]:
    normalized_target = capture_target.strip().lower()
    if normalized_target == "primary":
        return _primary_monitor_region(sct)
    if normalized_target == "all":
        return dict(sct.monitors[0])

    monitor_index = int(normalized_target)
    if monitor_index >= len(sct.monitors):
        raise ValueError(
            f"Monitor index {monitor_index} is not available "
            f"({len(sct.monitors) - 1} screen(s) attached)"
        )
    return dict(sct.monitors[monitor_index])


class WindowsCaptureBackend:
    """DXGI-first capture over a single long-lived ``MSS`` session."""

    def __init__(self) -> None:
        self._session = MSS()

    def grab(self, capture_target: str) -> tuple[bytes, int, int]:
        region = _capture_region(self._session, capture_target)
        captured = _dxcam_capture(region)
        if captured is not None:
            return captured
        screenshot = self._session.grab(region)
        return screenshot.raw, screenshot.width, screenshot.height

    def list_monitors(self) -> list[Monitor]:
        return [
            Monitor(
                index=index,
                left=int(monitor.get("left", 0)),
                top=int(monitor.get("top", 0)),
                width=int(monitor.get("width", 0)),
                height=int(monitor.get("height", 0)),
                is_primary=bool(monitor.get("is_primary", False)),
                name=str(monitor.get("output", "")),
            )
            for index, monitor in enumerate(self._session.monitors)
        ]

    def grab_png(self, capture_target: str, max_width: int | None = None) -> tuple[bytes, int, int]:
        raw, width, height = self.grab(capture_target)
        rgb = bgrx_to_rgb(raw, width, height)
        if max_width is not None:
            step = max(1, -(-width // max(1, max_width)))
            rgb, width, height = stride_rgb(rgb, width, height, step)
        return rgb_to_png(rgb, (width, height)), width, height

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            LOGGER.debug("MSS session failed to close cleanly", exc_info=True)
