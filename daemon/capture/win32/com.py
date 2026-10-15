"""Small COM ABI helper, deliberately independent of platform DLLs."""
import ctypes
from functools import lru_cache

@lru_cache(maxsize=None)
def _prototype(index, restype, argtypes):
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)

def vtcall(ptr, index, restype, argtypes, *args):
    """Invoke a COM vtable entry, caching the ABI prototype."""
    if not ptr: raise ValueError("null COM pointer")
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = _prototype(index, restype, tuple(argtypes))(vtbl[index])
    return fn(ptr, *args)

def com_release(ptr):
    if not ptr: return 0
    return vtcall(ptr, 2, ctypes.c_ulong, [])

def hr_ok(hr): return (int(hr) & 0x80000000) == 0

def hr_name(hr):
    return {0x887A0005: "DXGI_ERROR_DEVICE_REMOVED", 0x887A0006: "DXGI_ERROR_DEVICE_HUNG", 0x887A0007: "DXGI_ERROR_DEVICE_RESET"}.get(int(hr) & 0xffffffff, f"HRESULT(0x{int(hr) & 0xffffffff:08X})")

# Backward-compatible names.
vtable = vtcall
release = com_release
