"""Lazy DLL handles; importing this module is safe on every platform."""
import ctypes
user32 = kernel32 = d3d11 = dxgi = None
if __import__('sys').platform == 'win32':
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        d3d11 = ctypes.WinDLL('d3d11', use_last_error=True)
        dxgi = ctypes.WinDLL('dxgi', use_last_error=True)
    except Exception:
        user32 = kernel32 = d3d11 = dxgi = None

def require(handle, name: str):
    if handle is None: raise RuntimeError(f'{name} is only available on Windows')
    return handle
