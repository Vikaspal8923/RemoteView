"""Small COM ABI helper, deliberately independent of platform DLLs."""
import ctypes

def vtable(obj, index: int, result, *args):
    """Call COM method ``index`` on an interface pointer."""
    ptr = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    address = ptr.contents[index]
    fn = ctypes.CFUNCTYPE(result, ctypes.c_void_p, *args)(address)
    return fn(obj)

def release(obj) -> int:
    return int(vtable(obj, 2, ctypes.c_ulong))
