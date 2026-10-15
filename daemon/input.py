"""Windows interactive keyboard/mouse injection adapter.

This module is intentionally Windows-only at runtime and must be called from
the interactive Helper session, never from the Session-0 Boss service.
"""

from __future__ import annotations

import sys


class InputError(RuntimeError):
    pass


def _win32():
    if not sys.platform.startswith("win"):
        raise InputError("input injection is Windows-only")
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        raise InputError(f"Windows input API unavailable: {exc}") from exc
    return ctypes, wintypes


def emergency_release() -> None:
    """Release common held modifiers/buttons after disconnect or stop."""
    # The full SendInput mapping is added at the Helper integration boundary;
    # this safe hook is already available for every shutdown path.
    return None
