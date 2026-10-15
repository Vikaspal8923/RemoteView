"""Win32/DXGI declarations shared by the lazy Windows adapters."""
import ctypes

WDA_NONE = 0x0; WDA_MONITOR = 0x1; WDA_EXCLUDEFROMCAPTURE = 0x11
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D_DRIVER_TYPE_UNKNOWN = 0
DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_FORMAT_R8G8B8A8_UNORM = 28; DXGI_FORMAT_B8G8R8A8_UNORM = 87; DXGI_FORMAT_B8G8R8X8_UNORM = 88
IID_ID3D11_TEXTURE2D = "6f15aaf2-d208-4e89-9ab4-489535d34f9c"
IID_IDXGI_KEYED_MUTEX = "9d8e1289-d7b3-465f-8126-250e349af85d"

class LUID(ctypes.Structure): _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]
class RECT(ctypes.Structure): _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
class POINT(ctypes.Structure): _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
class DXGI_SAMPLE_DESC(ctypes.Structure): _fields_ = [("Count", ctypes.c_uint), ("Quality", ctypes.c_uint)]
class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint), ("MipLevels", ctypes.c_uint), ("ArraySize", ctypes.c_uint), ("Format", ctypes.c_uint), ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", ctypes.c_uint), ("BindFlags", ctypes.c_uint), ("CPUAccessFlags", ctypes.c_uint), ("MiscFlags", ctypes.c_uint)]
class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure): _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint), ("DepthPitch", ctypes.c_uint)]
