"""Screen capture loop (runs in the interactive user's session on Windows).

Uses ``mss`` to grab the configured monitor, compresses each frame to JPEG
with Pillow at the configured quality, and pushes ``(frame_bytes,
timestamp_ms)`` into an asyncio.Queue. Respects ``frame_rate`` (capture FPS).

This module is transport-agnostic: it fills a frame *sink*. On Linux the sink
is an asyncio.Queue feeding the in-process streamer; on Windows the sink is a
loopback-socket writer feeding the Session-0 Boss. See daemon/main.py.
"""

from __future__ import annotations

import asyncio
import io
import time
from typing import Optional, Protocol

import mss
import mss.tools
from PIL import Image

# A frame sink accepts (frame_bytes, timestamp_ms) via a *synchronous* put().
# Both the asyncio.Queue adapter (Linux) and the Windows socket transport writer
# implement this synchronous contract -- capture_loop never awaits the sink.
class FrameSink(Protocol):
    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None: ...


class QueueFrameSink:
    """Synchronous FrameSink backed by an asyncio.Queue.

    capture_loop is a plain coroutine that must not block on a full queue, so
    we use put_nowait() and drop the oldest frame when full (live-stream
    semantics: newest frames matter most). This keeps the capture loop and the
    transport decoupled and works identically for the Linux single-process
    mode and (with a different sink) the Windows Helper.
    """

    def __init__(self, queue):
        self._queue = queue

    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None:
        try:
            self._queue.put_nowait((frame_bytes, timestamp_ms))
        except Exception:
            # Queue full: drop the oldest to make room for the newest frame.
            try:
                self._queue.get_nowait()
            except Exception:
                pass
            try:
                self._queue.put_nowait((frame_bytes, timestamp_ms))
            except Exception:
                pass


class CaptureConfig:
    """Minimal capture settings pulled from the shared Config."""

    def __init__(self, frame_rate: int = 5, quality: int = 45, monitor: int = 0,
                 max_width: int = 1280, max_height: int = 720):
        self.frame_rate = frame_rate
        self.quality = quality
        self.monitor = monitor
        self.max_width = max_width
        self.max_height = max_height


def compress_to_jpeg(pil_image: Image.Image, quality: int) -> bytes:
    """Compress a PIL image to JPEG bytes at the given quality."""
    buf = io.BytesIO()
    # optimize=False keeps latency low; we stream at ~10 FPS.
    pil_image.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()


async def capture_loop(
    cfg: CaptureConfig,
    sink: FrameSink,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Continuously capture the screen and push JPEG frames to ``sink``.

    Runs until ``stop_event`` is set (or forever if None). Captures at
    ~``frame_rate`` FPS by sleeping ``1/frame_rate`` between grabs. A slow
    consumer simply drops frames (the queue/pipe bounds backpressure); we do
    not block the capture loop on a full sink.
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    interval = 1.0 / max(1, cfg.frame_rate)
    last_ts_ms = 0
    min_delta_ms = 1  # guard against duplicate timestamps

    with mss.mss() as sct:
        monitors = sct.monitors
        # monitors[0] is the "all monitors" virtual screen; user monitor index
        # (0 = primary in config) maps to monitors[1:]. Clamp defensively.
        idx = cfg.monitor + 1
        if idx < 1 or idx >= len(monitors):
            idx = 1
        monitor = monitors[idx]

        while not stop_event.is_set():
            start = time.monotonic()

            # grab() returns raw BGRA; convert to PIL and compress.
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            # Downscale before JPEG compression.  Keeping the aspect ratio
            # materially reduces CPU, bandwidth, and viewer decode latency.
            max_size = (cfg.max_width, cfg.max_height)
            if max_size[0] > 0 and max_size[1] > 0 and (
                img.width > max_size[0] or img.height > max_size[1]
            ):
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
            frame_bytes = compress_to_jpeg(img, cfg.quality)

            now_ms = int(time.time() * 1000)
            if now_ms <= last_ts_ms:
                now_ms = last_ts_ms + min_delta_ms
            last_ts_ms = now_ms

            try:
                sink.put(frame_bytes, now_ms)
            except Exception:
                # A full/slow sink should never kill capture; drop this frame.
                pass

            # Sleep for the remainder of the interval (accounting for work time).
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, interval - elapsed))
