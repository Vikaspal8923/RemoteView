"""Read-only Windows display-affinity and z-order inspection."""
from __future__ import annotations

import ctypes
import logging
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)
WDA_NONE = 0
GW_HWNDPREV = 3

@dataclass(frozen=True)
class Rect:
    left: int; top: int; right: int; bottom: int
    @property
    def width(self) -> int: return max(0, self.right - self.left)
    @property
    def height(self) -> int: return max(0, self.bottom - self.top)

@dataclass(frozen=True)
class ProtectedWindow:
    hwnd: int; affinity: int; class_name: str; title: str; client_rect: Rect; z_index: int

def subtract_rect(rect: Rect, cutter: Rect) -> list[Rect]:
    """Subtract intersection of cutter from rect, returning up to four pieces."""
    l, t = max(rect.left, cutter.left), max(rect.top, cutter.top)
    r, b = min(rect.right, cutter.right), min(rect.bottom, cutter.bottom)
    if l >= r or t >= b:
        return [rect]
    pieces = []
    if rect.top < t: pieces.append(Rect(rect.left, rect.top, rect.right, t))
    if b < rect.bottom: pieces.append(Rect(rect.left, b, rect.right, rect.bottom))
    if rect.left < l: pieces.append(Rect(rect.left, t, l, b))
    if r < rect.right: pieces.append(Rect(r, t, rect.right, b))
    return [p for p in pieces if p.width and p.height]

def subtract_rects(rect: Rect, cutters: list[Rect]) -> list[Rect]:
    pieces = [rect]
    for cutter in cutters:
        pieces = [piece for current in pieces for piece in subtract_rect(current, cutter)]
    return pieces

def _win():
    if not hasattr(ctypes, "windll"):
        return None
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    u.GetWindowDisplayAffinity.restype = ctypes.c_int
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]; u.IsWindowVisible.restype = ctypes.c_int
    u.IsIconic.argtypes = [ctypes.c_void_p]; u.IsIconic.restype = ctypes.c_int
    u.IsWindow.argtypes = [ctypes.c_void_p]; u.IsWindow.restype = ctypes.c_int
    u.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]; u.GetClientRect.restype = ctypes.c_int
    u.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.c_void_p]; u.ClientToScreen.restype = ctypes.c_int
    u.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]; u.GetClassNameW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]; u.GetWindowTextW.restype = ctypes.c_int
    return u

def scan() -> list[ProtectedWindow]:
    u = _win()
    if u is None: return []
    result: list[ProtectedWindow] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd): return 1
        affinity = ctypes.c_uint()
        if not u.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)) or not affinity.value: return 1
        class_buf = ctypes.create_unicode_buffer(256); title_buf = ctypes.create_unicode_buffer(512)
        u.GetClassNameW(hwnd, class_buf, len(class_buf)); u.GetWindowTextW(hwnd, title_buf, len(title_buf))
        class POINT(ctypes.Structure): _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        class CR(ctypes.Structure): _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = CR()
        if not u.GetClientRect(hwnd, ctypes.byref(r)): return 1
        p1, p2 = POINT(r.left, r.top), POINT(r.right, r.bottom)
        if not u.ClientToScreen(hwnd, ctypes.byref(p1)) or not u.ClientToScreen(hwnd, ctypes.byref(p2)): return 1
        rect = Rect(p1.x, p1.y, p2.x, p2.y)
        if rect.width and rect.height: result.append(ProtectedWindow(int(hwnd), affinity.value, class_buf.value, title_buf.value, rect, len(result)))
        return 1
    u.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]; u.EnumWindows.restype = ctypes.c_int
    u.EnumWindows(WNDENUMPROC(visit), 0)
    return result

def poll_loop(interval_ms: int, callback: Callable[[list[ProtectedWindow]], None], stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        callback(scan()); stop_event.wait(max(1, interval_ms) / 1000.0)
