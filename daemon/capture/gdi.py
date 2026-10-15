"""The original mss/GDI capture path."""
from __future__ import annotations

from PIL import Image
import time
from .backend import CaptureBackend


class GdiBackend(CaptureBackend):
    def __init__(self, monitor: int = 0):
        import mss
        self._sct = mss.mss()
        monitors = self._sct.monitors
        index = monitor + 1
        self.monitor = monitors[index if 1 <= index < len(monitors) else 1]

    def capture(self) -> Image.Image:
        last_error = None
        for attempt in range(3):
            try:
                shot = self._sct.grab(self.monitor)
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
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
        raise last_error

    def close(self) -> None:
        self._sct.close()
