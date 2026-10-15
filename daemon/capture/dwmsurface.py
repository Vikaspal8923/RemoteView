"""DWM shared-surface readback helpers."""
from __future__ import annotations
import ctypes, logging, threading
import numpy as np
from .win32.consts import *
from .win32.api import user32, kernel32, d3d11, dxgi
from .win32.com import vtcall, com_release, hr_ok, hr_name
log=logging.getLogger(__name__); _bad_formats=set()
def to_rgb(mapped, desc):
    if not mapped.pData or not desc.Width or not desc.Height: return None
    if desc.Format not in (DXGI_FORMAT_B8G8R8A8_UNORM,DXGI_FORMAT_R8G8B8A8_UNORM,DXGI_FORMAT_B8G8R8X8_UNORM):
        if desc.Format not in _bad_formats: _bad_formats.add(desc.Format); log.warning("unsupported DWM surface format %s",desc.Format)
        return None
    buf=np.frombuffer(ctypes.string_at(mapped.pData,desc.Height*mapped.RowPitch),dtype=np.uint8)
    img=buf.reshape(desc.Height,mapped.RowPitch)[:,:desc.Width*4].reshape(desc.Height,desc.Width,4).copy()
    if desc.Format in (DXGI_FORMAT_B8G8R8A8_UNORM,DXGI_FORMAT_B8G8R8X8_UNORM): img=img[:,:, [2,1,0,3]]
    if desc.Format==DXGI_FORMAT_B8G8R8X8_UNORM: img[:,:,3]=255
    return img
class DwmSurfaceReader:
    def __init__(self):
        self._lock=threading.RLock(); self._devices={}; self._contexts={}; self._warned=set()
        self._dwm=None
        if user32 is not None and kernel32 is not None:
            try:
                address=kernel32.GetProcAddress(user32._handle,b'DwmGetDxSharedSurface')
                if address: self._dwm=ctypes.WINFUNCTYPE(ctypes.c_int,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(LUID),ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint64))(address)
            except Exception: pass
    def _device_for(self,luid):
        key=(int(luid.HighPart)<<32)|(int(luid.LowPart)&0xffffffff)
        if key in self._devices: return self._devices[key],self._contexts[key]
        if dxgi is None or d3d11 is None: return None
        factory=ctypes.c_void_p()
        create=dxgi.CreateDXGIFactory1; create.argtypes=[ctypes.POINTER(GUID),ctypes.POINTER(ctypes.c_void_p)]; create.restype=ctypes.c_long
        hr=create(ctypes.byref(IID_IDXGI_FACTORY1),ctypes.byref(factory))
        if not hr_ok(hr): log.warning("CreateDXGIFactory1 failed: %s",hr_name(hr)); return None
        adapter=None
        try:
            i=0
            while True:
                a=ctypes.c_void_p(); hr=vtcall(factory,12,ctypes.c_long,[ctypes.c_uint,ctypes.POINTER(ctypes.c_void_p)],i,ctypes.byref(a))
                if not hr_ok(hr): break
                desc=DXGI_ADAPTER_DESC1(); vtcall(a,11,None,[ctypes.POINTER(DXGI_ADAPTER_DESC1)],ctypes.byref(desc))
                if desc.AdapterLuid.LowPart==luid.LowPart and desc.AdapterLuid.HighPart==luid.HighPart: adapter=a; break
                com_release(a); i+=1
            if adapter is None: log.warning("no DXGI adapter matches DWM surface LUID %08x:%08x",luid.HighPart & 0xffffffff,luid.LowPart); return None
            dev=ctypes.c_void_p(); ctx=ctypes.c_void_p(); levels=(ctypes.c_uint*1)(D3D_FEATURE_LEVEL_11_0)
            d3d11.D3D11CreateDevice.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]; d3d11.D3D11CreateDevice.restype=ctypes.c_long
            hr=d3d11.D3D11CreateDevice(adapter,D3D_DRIVER_TYPE_UNKNOWN,None,D3D11_CREATE_DEVICE_BGRA_SUPPORT,levels,1,D3D11_SDK_VERSION,ctypes.byref(dev),None,ctypes.byref(ctx))
            if not hr_ok(hr): log.warning("D3D11CreateDevice failed: %s",hr_name(hr)); return None
            self._devices[key]=dev; self._contexts[key]=ctx; return dev,ctx
        finally: com_release(factory)
    def grab(self, hwnd):
        if __import__('sys').platform!='win32' or self._dwm is None:
            log.warning("DWM shared-surface export unavailable")
            return None
        with self._lock:
            surf=ctypes.c_void_p(); luid=LUID(); fmt=ctypes.c_uint(); flags=ctypes.c_uint(); upd=ctypes.c_uint64()
            if not self._dwm(hwnd,ctypes.byref(surf),ctypes.byref(luid),ctypes.byref(fmt),ctypes.byref(flags),ctypes.byref(upd)) or not surf.value:
                log.debug("no DWM surface for hwnd %s", hwnd); return None
            pair=self._device_for(luid)
            if pair is None: return None
            dev,ctx=pair; tex=ctypes.c_void_p()
            hr=vtcall(dev,28,ctypes.c_long,[ctypes.c_void_p,ctypes.POINTER(GUID),ctypes.POINTER(ctypes.c_void_p)],surf,ctypes.byref(IID_ID3D11_TEXTURE2D),ctypes.byref(tex))
            if not hr_ok(hr): log.warning("OpenSharedResource failed: %s",hr_name(hr)); return None
            try:
                desc=D3D11_TEXTURE2D_DESC(); vtcall(tex,10,None,[ctypes.POINTER(D3D11_TEXTURE2D_DESC)],ctypes.byref(desc))
                mutex=ctypes.c_void_p(); has_mutex=hr_ok(vtcall(tex,0,ctypes.c_long,[ctypes.POINTER(GUID),ctypes.POINTER(ctypes.c_void_p)],ctypes.byref(IID_IDXGI_KEYED_MUTEX),ctypes.byref(mutex)))
                if has_mutex:
                    hr=vtcall(mutex,8,ctypes.c_long,[ctypes.c_uint64,ctypes.c_uint],0,0)
                    if not hr_ok(hr): com_release(mutex); log.warning("AcquireSync failed: %s",hr_name(hr)); return None
                staging=ctypes.c_void_p(); sd= D3D11_TEXTURE2D_DESC(); ctypes.memmove(ctypes.byref(sd),ctypes.byref(desc),ctypes.sizeof(desc)); sd.Usage=3; sd.BindFlags=0; sd.CPUAccessFlags=0x20000; sd.MiscFlags=0
                hr=vtcall(dev,5,ctypes.c_long,[ctypes.POINTER(D3D11_TEXTURE2D_DESC),ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)],ctypes.byref(sd),None,ctypes.byref(staging))
                if not hr_ok(hr):
                    if has_mutex: vtcall(mutex,9,ctypes.c_long,[ctypes.c_uint64],0); com_release(mutex)
                    log.warning("CreateTexture2D staging failed: %s",hr_name(hr)); return None
                try:
                    vtcall(ctx,47,None,[ctypes.c_void_p,ctypes.c_void_p],staging,tex)
                    if has_mutex: vtcall(mutex,9,ctypes.c_long,[ctypes.c_uint64],0)
                    mapped=D3D11_MAPPED_SUBRESOURCE(); hr=vtcall(ctx,14,ctypes.c_long,[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],staging,0,1,0,ctypes.byref(mapped))
                    if not hr_ok(hr): log.warning("Map staging texture failed: %s",hr_name(hr)); return None
                    try: return to_rgb(mapped,desc)
                    finally: vtcall(ctx,15,None,[ctypes.c_void_p,ctypes.c_uint],staging,0)
                finally:
                    com_release(staging)
                    if has_mutex: com_release(mutex)
            finally: com_release(tex)
    def read(self, hwnd):
        return self.grab(hwnd)
def read_surface(hwnd): return DwmSurfaceReader().grab(hwnd)
