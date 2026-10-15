"""Optional DWM shared-surface reader (Windows implementation hook)."""
from __future__ import annotations
import ctypes

AVAILABLE = False
if hasattr(ctypes, "windll"):
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        AVAILABLE = bool(_user32.GetProcAddress(_user32._handle, b"DwmGetDxSharedSurface"))
    except Exception:
        AVAILABLE = False

def read_surface(hwnd: int):
    """Return an RGB array for ``hwnd`` or None when unavailable."""
    return None
