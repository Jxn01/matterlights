"""Linux capture: a PipeWire node rendered into BGRx frames by GStreamer.

WHY NOT mss. On GNOME Wayland, X11 screen capture returns a **black frame**.
Measured on this rig 2026-09-08: ``mss`` enumerates all three monitors with
correct geometry and every single pixel of every grab is zero. That is not a bug
in mss -- XWayland is a real X server and knows the layout Mutter tells it, but
native Wayland windows are never composited into the X11 root window, so there is
simply nothing in the buffer to read. Geometry is metadata; pixels are content,
and XWayland has only the first.

So capture goes through the compositor: a ScreenCast session yields a PipeWire
node, GStreamer turns that node into frames, and ``appsink`` hands them over as
BGRx -- the exact byte order the colour sampler already indexes.

TWO THINGS THAT WOULD OTHERWISE BITE:

*Frame rate.* The stream is damage-driven, so a static desktop costs nothing,
but a video or a game will free-run at the monitor's refresh rate. Uncapped that
is ~1.4 GB/s of memcpy for a 4K frame, to produce colours sampled at one pixel in
121 and then discarded. ``max-framerate`` in the caps is honoured by Mutter at
source -- measured 110.7 fps down to 4.7 fps -- and is derived from the sync
interval rather than hard-coded.

*Row stride.* A GStreamer video buffer is not necessarily packed: rows can be
padded to an alignment boundary, so the distance between rows is not always
``width * 4``. Reading it as packed shears the image diagonally, which looks like
a broken capture rather than an arithmetic mistake. The stride is taken from the
buffer's video meta and the frame repacked when it differs.
"""

from __future__ import annotations

import logging
import math
import threading

from matterlights.capture import Monitor, bgrx_to_rgb, rgb_to_png, stride_rgb
from matterlights.capture.mutter import MutterUnavailable

LOGGER = logging.getLogger("matterlights.capture.linux")

_DEFAULT_SYNC_INTERVAL_SECONDS = 0.2
_FIRST_FRAME_TIMEOUT_SECONDS = 10.0


def caps_framerate(sync_interval_seconds: float) -> int:
    """Frames per second to allow: twice the sampling rate, at least 1.

    Twice, so a frame is always fresher than the tick that reads it, without
    letting the stream run at vsync for a consumer that samples five times a
    second.
    """

    if sync_interval_seconds <= 0:
        return 1
    return max(1, math.ceil(2.0 / sync_interval_seconds))


def build_pipeline_description(node_id: int, sync_interval_seconds: float) -> str:
    return (
        f"pipewiresrc path={node_id} always-copy=true ! "
        f"videoconvert ! "
        f"video/x-raw,format=BGRx,max-framerate={caps_framerate(sync_interval_seconds)}/1 ! "
        f"appsink name=sink max-buffers=1 drop=true sync=false emit-signals=true"
    )


class LinuxCaptureBackend:
    """Capture through the compositor, one PipeWire stream at a time.

    Only one stream is held: switching capture target tears the old one down.
    That matches how the sync loop behaves -- it samples one target per tick --
    and avoids paying for screens nobody is looking at.
    """

    def __init__(
        self,
        source=None,
        sync_interval_seconds: float = _DEFAULT_SYNC_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or LOGGER
        self._sync_interval_seconds = sync_interval_seconds
        self._source = source if source is not None else self._default_source()
        self._source.set_invalidation_callback(self._invalidate)

        self._lock = threading.Lock()
        self._pipeline = None
        self._active_target: str | None = None
        self._latest: tuple[bytes, int, int] | None = None
        self._first_frame = threading.Event()
        self._invalidated = threading.Event()
        self._fallback_target: str | None = None
        self._expected_connector: str = ""

    def _default_source(self):
        from matterlights.capture.mutter import MutterSource

        return MutterSource(self._logger)

    # -- CaptureBackend ----------------------------------------------------

    def grab(self, capture_target: str) -> tuple[bytes, int, int]:
        self._ensure_stream(capture_target)
        with self._lock:
            latest = self._latest
        if latest is not None:
            return latest

        # Nothing yet: wait briefly for the very first frame, then give up and
        # let the caller retry on its own cadence.
        if self._first_frame.wait(_FIRST_FRAME_TIMEOUT_SECONDS):
            with self._lock:
                if self._latest is not None:
                    return self._latest
        raise RuntimeError("No frame arrived from the capture stream")

    def list_monitors(self) -> list[Monitor]:
        return self._source.list_monitors()

    def grab_png(self, capture_target: str, max_width: int | None = None) -> tuple[bytes, int, int]:
        raw, width, height = self.grab(capture_target)
        rgb = bgrx_to_rgb(raw, width, height)
        if max_width is not None:
            step = max(1, -(-width // max(1, max_width)))
            rgb, width, height = stride_rgb(rgb, width, height, step)
        return rgb_to_png(rgb, (width, height)), width, height

    def close(self) -> None:
        self._teardown_pipeline()
        try:
            self._source.close()
        except Exception:  # noqa: BLE001
            self._logger.debug("Capture source failed to close cleanly", exc_info=True)

    # -- stream lifecycle --------------------------------------------------

    def set_fallback_target(self, capture_target: str) -> None:
        """The target to fall back to when the requested one disappears.

        Mirrors the behaviour the README already promises on Windows: a screen
        that is unplugged or powered off logs a warning and the loop reverts to
        ``SCREEN_CAPTURE_TARGET`` until it returns.
        """

        self._fallback_target = capture_target

    def _invalidate(self, reason: str) -> None:
        self._invalidated.set()

    def _ensure_stream(self, capture_target: str) -> None:
        if self._invalidated.is_set():
            self._invalidated.clear()
            self._teardown_pipeline()
            self._active_target = None

        if self._pipeline is not None and self._active_target == capture_target:
            return

        with self._lock:
            self._latest = None
        self._first_frame.clear()
        self._teardown_pipeline()

        # If the physical screen we were recording has gone, say so and use the
        # configured default until it comes back. Checking the CONNECTOR rather
        # than re-resolving the index is what makes this real: unplug the middle
        # screen of three and index 2 still resolves happily -- to a different
        # monitor -- so an index check would never notice.
        expected = self._expected_connector
        if expected and not self._source.connector_present(expected):
            fallback = self._fallback_target
            if fallback is not None and fallback != capture_target:
                self._logger.warning(
                    "Screen %s is no longer attached; falling back to %r until it returns",
                    expected,
                    fallback,
                )
                capture_target_to_open = fallback
            else:
                capture_target_to_open = capture_target
        else:
            capture_target_to_open = capture_target

        try:
            node_id = self._source.open_stream(capture_target_to_open)
        except (ValueError, MutterUnavailable) as error:
            fallback = self._fallback_target
            if fallback is None or fallback == capture_target_to_open:
                raise
            self._logger.warning(
                "Capture target %r unavailable (%s); falling back to %r",
                capture_target_to_open,
                error,
                fallback,
            )
            node_id = self._source.open_stream(fallback)

        self._start_pipeline(node_id)
        # Keep the REQUESTED target, not the one actually opened, so a screen
        # that comes back is picked up again on the next rebuild.
        self._active_target = capture_target
        self._expected_connector = getattr(self._source, "active_connector", "")

    def _start_pipeline(self, node_id: int) -> None:
        Gst = _gst()
        description = build_pipeline_description(node_id, self._sync_interval_seconds)
        self._logger.info("Capture pipeline: %s", description)
        pipeline = Gst.parse_launch(description)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        pipeline.set_state(Gst.State.PLAYING)
        self._pipeline = pipeline

    def _teardown_pipeline(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is None:
            return
        try:
            Gst = _gst()
            pipeline.set_state(Gst.State.NULL)
        except Exception:  # noqa: BLE001
            self._logger.debug("Capture pipeline failed to stop cleanly", exc_info=True)

    def _on_sample(self, sink):
        """Store the newest frame. Runs on GStreamer's streaming thread.

        Deliberately does the copy here rather than in ``grab``: the sync loop's
        thread must never block on the pipeline, because it is the same thread
        that has to answer a shutdown signal promptly.
        """

        Gst = _gst()
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buffer = sample.get_buffer()
        structure = sample.get_caps().get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")

        ok, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            stride = _buffer_stride(buffer, width)
            raw = bytes(mapinfo.data)
            if stride != width * 4:
                raw = _repack(raw, width, height, stride)
            with self._lock:
                self._latest = (raw, width, height)
        finally:
            buffer.unmap(mapinfo)
        self._first_frame.set()
        return Gst.FlowReturn.OK


def _repack(raw: bytes, width: int, height: int, stride: int) -> bytes:
    packed = width * 4
    return b"".join(raw[y * stride : y * stride + packed] for y in range(height))


def _buffer_stride(buffer, width: int) -> int:
    """The real byte distance between rows, from the video meta when present."""

    try:
        from gi.repository import GstVideo

        meta = GstVideo.buffer_get_video_meta(buffer)
        if meta is not None and meta.n_planes > 0:
            return int(meta.stride[0])
    except Exception:  # noqa: BLE001 - no meta means it is packed
        pass
    return width * 4


_GST = None


def _gst():
    """Initialise GStreamer once, with an actionable error if it is missing."""

    global _GST
    if _GST is not None:
        return _GST
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        gi.require_version("GstVideo", "1.0")
        # GstApp and GstVideo are imported for their SIDE EFFECT: they register
        # the Python bindings for GstAppSink's methods and for the video meta.
        # Without GstApp, an appsink has no ``try_pull_sample`` and the
        # ``new-sample`` path is unverifiable; without GstVideo,
        # ``_buffer_stride`` silently falls back to width*4 on every frame and a
        # padded buffer shears the image.
        from gi.repository import Gst, GstApp, GstVideo  # noqa: F401
    except (ImportError, ValueError) as error:
        raise MutterUnavailable(
            "GStreamer is not importable. MatterLights needs the system packages "
            "'python-gobject', 'gstreamer', 'gst-plugins-base' and 'gst-plugin-pipewire', "
            "and a virtualenv created with --system-site-packages so it can see them. "
            f"Original error: {error}"
        ) from error
    if not Gst.is_initialized():
        Gst.init(None)
    _GST = Gst
    return Gst
