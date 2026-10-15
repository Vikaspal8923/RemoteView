"""Local-input priority for the interactive Windows Helper.

The low-level hooks observe physical keyboard/mouse activity and ignore
SendInput events marked as injected.  Remote control is therefore paused only
while the local user is active; the screen stream and service stay connected.
"""

from __future__ import annotations

import sys
import threading
import time
import ctypes
from ctypes import wintypes


class LocalInputArbiter:
    def __init__(self, idle_seconds: float = 1.5):
        self.idle_seconds = idle_seconds
        self._last_local = 0.0
        self._lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()
        self._hooks = []
        self._thread = None

    def start(self) -> None:
        if not sys.platform.startswith("win") or self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="local-input-arbiter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def remote_allowed(self) -> bool:
        with self._lock:
            return time.monotonic() - self._last_local >= self.idle_seconds

    def local_active(self) -> bool:
        return not self.remote_allowed()

    def _mark_local(self):
        with self._lock:
            self._last_local = time.monotonic()

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)
        WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
        LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01

        class Kbd(ctypes.Structure):
            _fields_ = [("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                        ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                        ("extra", ctypes.c_void_p)]

        class Mouse(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("extra", ctypes.c_void_p)]

        @HOOKPROC
        def keyboard(n_code, w_param, l_param):
            if n_code >= 0 and l_param:
                info = ctypes.cast(l_param, ctypes.POINTER(Kbd)).contents
                if not info.flags & LLKHF_INJECTED:
                    self._mark_local()
            return user32.CallNextHookEx(0, n_code, w_param, l_param)

        @HOOKPROC
        def mouse(n_code, w_param, l_param):
            if n_code >= 0 and l_param:
                info = ctypes.cast(l_param, ctypes.POINTER(Mouse)).contents
                if not info.flags & LLMHF_INJECTED:
                    self._mark_local()
            return user32.CallNextHookEx(0, n_code, w_param, l_param)

        self._hooks = [keyboard, mouse]
        hmod = kernel32.GetModuleHandleW(None)
        khook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard, hmod, 0)
        mhook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse, hmod, 0)
        try:
            if khook:
                self._hooks.append(khook)
            if mhook:
                self._hooks.append(mhook)
            msg = wintypes.MSG()
            while not self._stop.is_set() and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if khook:
                user32.UnhookWindowsHookEx(khook)
            if mhook:
                user32.UnhookWindowsHookEx(mhook)

