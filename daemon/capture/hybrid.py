"""Injectable, numpy-based desktop/protected-window compositor."""
from __future__ import annotations
import numpy as np
import logging
from PIL import Image
from .affinity import subtract_rects
log = logging.getLogger(__name__)

class FakeReader:
    def read(self, hwnd):
        seed = int(hwnd) & 255
        return np.full((40, 60, 4), [seed, 80, 180, 255], dtype=np.uint8)

class HybridBackend:
    def __init__(self, reader=None, gdi=None, clock=None):
        self.reader = reader if reader is not None else FakeReader()
        self.gdi = gdi; self.clock = clock; self.windows = []; self.failures = {}; self.failed = set()
    def _base(self):
        image = self.gdi.capture() if self.gdi is not None else Image.new("RGB", (320, 240), (20, 20, 20))
        if isinstance(image, np.ndarray): image = Image.fromarray(image.astype(np.uint8))
        return np.asarray(image.convert("RGBA")).copy()
    def composite(self, protected=None, occluders=None, cursor=None):
        out = self._base(); protected = protected if protected is not None else self.windows
        occluders = occluders or []
        for pos, window in enumerate(protected):
            if window.hwnd in self.failed: continue
            try:
                if hasattr(self.reader, "read"):
                    surface = self.reader.read(window.hwnd)
                elif hasattr(self.reader, "grab"):
                    surface = self.reader.grab(window.hwnd)
                else:
                    surface = self.reader(window.hwnd)
            except Exception:
                surface = None
            if surface is None:
                self.failures[window.hwnd] = self.failures.get(window.hwnd, 0) + 1
                if self.failures[window.hwnd] >= 5: self.failed.add(window.hwnd)
                continue
            self.failures[window.hwnd] = 0
            surface = np.asarray(surface, dtype=np.uint8); rect = window.client_rect
            if surface.ndim != 3 or surface.shape[2] not in (3,4): continue
            if surface.shape[2] == 3:
                surface = np.concatenate([surface, np.full(surface.shape[:2]+(1,),255,np.uint8)],2)
            elif int(surface[:,:,3].max()) == 0:
                # Some DWM redirection surfaces expose opaque RGB with an
                # unused alpha plane. Treat that as opaque rather than making
                # the protected window disappear during source-over blending.
                surface = surface.copy(); surface[:,:,3] = 255
            if surface.shape[:2] != (rect.height, rect.width):
                surface = np.asarray(Image.fromarray(surface).resize((rect.width, rect.height), Image.Resampling.BILINEAR))
                if surface.ndim == 2: surface = surface[:,:,None]
                if surface.shape[2] == 3: surface = np.concatenate([surface, np.full(surface.shape[:2]+(1,),255,np.uint8)],2)
            higher = [w.client_rect for w in protected[:pos]]
            pasted = 0
            for piece in subtract_rects(rect, list(occluders) + higher):
                x0,y0,x1,y1 = piece.left,piece.top,piece.right,piece.bottom
                if x0 < 0 or y0 < 0 or x0 >= out.shape[1] or y0 >= out.shape[0]: continue
                w=min(x1-x0, out.shape[1]-x0, surface.shape[1]); h=min(y1-y0, out.shape[0]-y0, surface.shape[0])
                if w <= 0 or h <= 0: continue
                src=surface[:h,:w,:4].astype(np.float32); dst=out[y0:y0+h,x0:x0+w].astype(np.float32)
                a=src[:,:,3:4]/255.0; out[y0:y0+h,x0:x0+w,:3]=(src[:,:,:3]+dst[:,:,:3]*(1-a)).clip(0,255).astype(np.uint8); out[y0:y0+h,x0:x0+w,3]=255
                pasted += 1
            log.debug("composited hwnd=%s surface=%sx%s target=%sx%s subrects=%s", window.hwnd, surface.shape[1], surface.shape[0], rect.width, rect.height, pasted)
        return Image.fromarray(out[:,:,:3], "RGB")
    def capture(self): return self.composite()
    def close(self):
        if self.gdi is not None and hasattr(self.gdi, 'close'): self.gdi.close()
