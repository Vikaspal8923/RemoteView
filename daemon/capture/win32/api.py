"""Lazy DLL handles; importing this module is safe on every platform."""
import ctypes
user32 = kernel32 = d3d11 = dxgi = None
if __import__('sys').platform == 'win32':
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        d3d11 = ctypes.WinDLL('d3d11', use_last_error=True)
        dxgi = ctypes.WinDLL('dxgi', use_last_error=True)
        # HMODULE is pointer-sized; explicit declarations are required on
        # 64-bit Windows or ctypes truncates the DLL handle.
        kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p
    except Exception:
        user32 = kernel32 = d3d11 = dxgi = None

def require(handle, name: str):
    if handle is None: raise RuntimeError(f'{name} is only available on Windows')
    return handle

def exports():
    """Return the lazily loaded DLL export availability."""
    return {"user32": user32 is not None, "d3d11": d3d11 is not None, "dxgi": dxgi is not None}
