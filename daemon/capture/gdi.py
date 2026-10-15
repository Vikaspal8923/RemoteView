"""The original mss/GDI capture path."""
from __future__ import annotations

from PIL import Image
import time
import logging
from .backend import CaptureBackend
log = logging.getLogger(__name__)


class GdiBackend(CaptureBackend):
    def __init__(self, monitor: int = 0):
        import mss
        self._sct = mss.mss()
        monitors = self._sct.monitors
        index = monitor + 1
        self.monitor = monitors[index if 1 <= index < len(monitors) else 1]
        self._last_image = None
        self._bitblt_warned = False

    def capture(self) -> Image.Image:
        last_error = None
        for attempt in range(3):
            try:
                shot = self._sct.grab(self.monitor)
                self._last_image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                return self._last_image.copy()
            except Exception as exc:
                last_error = exc
                # BitBlt can transiently time out while DWM is rebuilding a
                # protected surface. Recreate the MSS context before retrying.
                if attempt < 2:
                    try: self._sct.close()
                    except Exception: pass
                    import mss
                    self._sct = mss.mss()
                    time.sleep(0.05 * (attempt + 1))
        if not self._bitblt_warned:
            log.warning("GDI BitBlt unavailable; reusing last desktop frame: %s", last_error)
            self._bitblt_warned = True
        if self._last_image is not None:
            return self._last_image.copy()
        width = max(1, int(self.monitor.get("width", 1)))
        height = max(1, int(self.monitor.get("height", 1)))
        return Image.new("RGB", (width, height), (0, 0, 0))

    def close(self) -> None:
        self._sct.close()
