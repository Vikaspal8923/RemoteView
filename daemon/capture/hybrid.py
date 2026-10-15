"""Injectable, numpy-based desktop/protected-window compositor."""
from __future__ import annotations
import numpy as np
from PIL import Image
from .affinity import subtract_rects

class FakeReader:
    def read(self, hwnd):
        seed = int(hwnd) & 255
        return np.full((40, 60, 4), [seed, 80, 180, 255], dtype=np.uint8)

class HybridBackend:
    def __init__(self, reader=None, gdi=None, clock=None):
        self.reader = reader if reader is not None else FakeReader()
        self.gdi = gdi; self.clock = clock; self.windows = []
    def _base(self):
        image = self.gdi.capture() if self.gdi is not None else Image.new("RGB", (320, 240), (20, 20, 20))
        return np.asarray(image.convert("RGBA")).copy()
    def composite(self, protected=None, occluders=None, cursor=None):
        out = self._base(); protected = protected if protected is not None else self.windows
        occluders = occluders or []
        for window in protected:
            surface = self.reader.read(window.hwnd)
            if surface is None: continue
            surface = np.asarray(surface, dtype=np.uint8); rect = window.client_rect
            for piece in subtract_rects(rect, occluders):
                x0,y0,x1,y1 = piece.left,piece.top,piece.right,piece.bottom
                if x0 < 0 or y0 < 0 or x0 >= out.shape[1] or y0 >= out.shape[0]: continue
                w=min(x1-x0, out.shape[1]-x0, surface.shape[1]); h=min(y1-y0, out.shape[0]-y0, surface.shape[0])
                if w <= 0 or h <= 0: continue
                src=surface[:h,:w,:4].astype(np.float32); dst=out[y0:y0+h,x0:x0+w].astype(np.float32)
                a=src[:,:,3:4]/255.0; out[y0:y0+h,x0:x0+w,:3]=(src[:,:,:3]*a+dst[:,:,:3]*(1-a)).astype(np.uint8); out[y0:y0+h,x0:x0+w,3]=255
        return Image.fromarray(out[:,:,:3], "RGB")
    def capture(self): return self.composite()
    def close(self):
        if self.gdi is not None and hasattr(self.gdi, 'close'): self.gdi.close()
