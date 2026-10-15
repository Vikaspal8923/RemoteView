"""Hybrid compositor. Surface readback is intentionally optional on non-Windows."""
from __future__ import annotations
from .gdi import GdiBackend

class HybridBackend(GdiBackend):
    """GDI base with an optional affinity overlay provider."""
    pass
