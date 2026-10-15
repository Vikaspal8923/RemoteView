"""Windows interactive keyboard/mouse injection adapter.

This module is intentionally Windows-only at runtime and must be called from
the interactive Helper session, never from the Session-0 Boss service.
"""

from __future__ import annotations

import sys
import ctypes


class InputError(RuntimeError):
    pass


def _win32():
    if not sys.platform.startswith("win"):
        raise InputError("input injection is Windows-only")
    try:
        from ctypes import wintypes
    except Exception as exc:
        raise InputError(f"Windows input API unavailable: {exc}") from exc
    return ctypes, wintypes


def emergency_release() -> None:
    """Release common held modifiers/buttons after disconnect or stop."""
    if not sys.platform.startswith("win"):
        return
    ctypes, wintypes = _win32()
    user32 = ctypes.windll.user32
    # Release modifiers and mouse buttons defensively.
    for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        _key(vk, False)
    for flag in (0x0004, 0x0008, 0x0010, 0x0020):
        _mouse(0, 0, 0, flag)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send(inp):
    if ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
        raise InputError("SendInput failed")


def _key(vk: int, down: bool):
    inp = _INPUT(type=1, ki=_KEYBDINPUT(wVk=vk, dwFlags=0 if down else 2))
    _send(inp)


def _mouse(dx: int, dy: int, data: int, flags: int):
    inp = _INPUT(type=0, mi=_MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags))
    _send(inp)


def move_normalized(x: float, y: float):
    if not 0 <= x <= 1 or not 0 <= y <= 1:
        raise InputError("mouse coordinates out of range")
    user32 = ctypes.windll.user32
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    _mouse(round(x * 65535), round(y * 65535), 0, 0x8000 | 0x0001)


def mouse_button(button: str, down: bool):
    flags = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}
    if button not in flags:
        raise InputError("unsupported mouse button")
    _mouse(0, 0, 0, flags[button][0 if down else 1])


def scroll(delta: int):
    _mouse(0, 0, delta * 120, 0x0800)


def key_virtual(vk: int, down: bool):
    _key(vk, down)


def key_name(name: str, down: bool):
    """Inject a conservative named key using Windows virtual-key values."""
    names = {
        "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12, "ENTER": 0x0D,
        "ESC": 0x1B, "TAB": 0x09, "BACKSPACE": 0x08, "SPACE": 0x20,
        "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
        "DELETE": 0x2E, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "CONTROL_L": 0x11, "CONTROL_R": 0x11,
        "SHIFT_L": 0x10, "SHIFT_R": 0x10,
        "ALT_L": 0x12, "ALT_R": 0x12,
        "RETURN": 0x0D, "KP_ENTER": 0x0D,
        "ESCAPE": 0x1B, "DEL": 0x2E,
        "CAPS_LOCK": 0x14, "SUPER_L": 0x5B, "SUPER_R": 0x5C,
    }
    key = name.upper()
    vk = names.get(key)
    if vk is None and len(key) == 1:
        vk = ord(key)
    if vk is None or not 0 <= vk <= 255:
        raise InputError(f"unsupported key: {name}")
    key_virtual(vk, down)


def type_text(text: str):
    """Type basic Unicode text through the Windows Unicode SendInput path."""
    if len(text) > 256:
        raise InputError("text is too long")
    for char in text:
        code = ord(char)
        down = _INPUT(type=1, ki=_KEYBDINPUT(wVk=0, wScan=code, dwFlags=4))
        up = _INPUT(type=1, ki=_KEYBDINPUT(wVk=0, wScan=code, dwFlags=4 | 2))
        _send(down)
        _send(up)
