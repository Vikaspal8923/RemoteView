"""Capture dispatcher; keeps the historical capture_loop API stable."""
from __future__ import annotations
import asyncio, io, time
from typing import Optional, Protocol
from PIL import Image

class FrameSink(Protocol):
    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None: ...

class QueueFrameSink:
    def __init__(self, queue): self._queue = queue
    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None:
        try: self._queue.put_nowait((frame_bytes, timestamp_ms))
        except Exception:
            try: self._queue.get_nowait()
            except Exception: pass
            try: self._queue.put_nowait((frame_bytes, timestamp_ms))
            except Exception: pass

class CaptureConfig:
    def __init__(self, frame_rate=5, quality=45, monitor=0, max_width=1280, max_height=720, capture_backend="auto", affinity_enabled=True, affinity_poll_ms=1000, affinity_max_failures=5):
        self.frame_rate=frame_rate; self.quality=quality; self.monitor=monitor; self.max_width=max_width; self.max_height=max_height
        self.capture_backend=capture_backend; self.affinity_enabled=affinity_enabled; self.affinity_poll_ms=affinity_poll_ms; self.affinity_max_failures=affinity_max_failures

def compress_to_jpeg(image: Image.Image, quality: int) -> bytes:
    buf=io.BytesIO(); image.save(buf, format="JPEG", quality=quality, optimize=False); return buf.getvalue()

async def capture_loop(cfg: CaptureConfig, sink: FrameSink, stop_event: Optional[asyncio.Event]=None) -> None:
    stop_event = stop_event or asyncio.Event(); interval=1.0/max(1,cfg.frame_rate); last=0
    # Keep optional Windows capture dependencies out of import-time paths.
    from .gdi import GdiBackend
    from .hybrid import HybridBackend
    backend = GdiBackend(cfg.monitor) if cfg.capture_backend == "gdi" else HybridBackend(gdi=GdiBackend(cfg.monitor))
    try:
        while not stop_event.is_set():
            start=time.monotonic(); img=backend.capture()
            if cfg.max_width>0 and cfg.max_height>0 and (img.width>cfg.max_width or img.height>cfg.max_height): img.thumbnail((cfg.max_width,cfg.max_height), Image.Resampling.LANCZOS)
            now=max(int(time.time()*1000), last+1); last=now
            try: sink.put(compress_to_jpeg(img,cfg.quality), now)
            except Exception: pass
            await asyncio.sleep(max(0, interval-(time.monotonic()-start)))
    finally: backend.close()
