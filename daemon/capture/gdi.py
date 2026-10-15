"""The original mss/GDI capture path."""
from __future__ import annotations

from PIL import Image
from .backend import CaptureBackend


class GdiBackend(CaptureBackend):
    def __init__(self, monitor: int = 0):
        import mss
        self._sct = mss.mss()
        monitors = self._sct.monitors
        index = monitor + 1
        self.monitor = monitors[index if 1 <= index < len(monitors) else 1]

    def capture(self) -> Image.Image:
        shot = self._sct.grab(self.monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def close(self) -> None:
        self._sct.close()
