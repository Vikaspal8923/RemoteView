"""Named-pipe frame transport between the Boss and the Helper (Windows).

Why a pipe? Session 0 isolation means the un-killable Boss service cannot see
the interactive desktop, so the capture Helper must run in the user's session.
The Helper captures frames and sends them to the Boss over a local, ACL-gated
named pipe; the Boss relays them to the viewer over TLS.

Frame format on the pipe reuses shared.protocol.pack_frame (uint32 size +
JPEG payload + uint64 unix_ms), so the wire format is identical end to end.

This module is import-safe on non-Windows: the pywin32 imports are lazy and
only the transport functions need Windows. Constants and the SDDL builder are
cross-platform so they can be unit-tested on Linux.
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

from shared import protocol as proto

# Canonical pipe name. The \\.\pipe\ prefix is the local named-pipe namespace.
PIPE_NAME = r"\\.\pipe\RemoteViewFrames"

# SDDL for the pipe's security descriptor:
#   D:                -> a protected DACL (no inheritance from parent)
#   (A;;FA;;;SY)      -> SYSTEM (the Boss, LocalSystem): full access
#   (A;;FA;;;BA)      -> BUILTIN\Administrators: full access
#   (A;;GW;;;AU)      -> Authenticated Users: generic WRITE (the per-session
#                        Helper runs as the interactive user and only writes)
# The pipe is INBOUND (server reads), so the client needs write access only.
PIPE_SDDL = "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;GW;;;AU)"

# Generous read buffer for the server (a single large frame must fit).
_PIPE_BUFSIZE = 64 * 1024

_pywin32_available = False
try:  # pragma: no cover - import guard, only meaningful on Windows
    import win32file  # type: ignore
    import win32pipe  # type: ignore
    import win32security  # type: ignore
    import pywintypes  # type: ignore

    _pywin32_available = True
except Exception:  # ModuleNotFoundError on Linux/macOS
    win32file = None  # type: ignore
    win32pipe = None  # type: ignore
    win32security = None  # type: ignore
    pywintypes = None  # type: ignore


def require_windows() -> None:
    """Raise a clear error if the pipe transport is used off Windows."""
    if not _pywin32_available:
        raise RuntimeError(
            "RemoteView named-pipe transport requires Windows + pywin32; "
            "this code path is Windows-only."
        )


def build_pipe_sddl(interactive_only: bool = True) -> str:
    """Return the SDDL used to protect the frame pipe.

    Kept as a pure function so the security posture is reviewable and testable
    without a Windows host.
    """
    if interactive_only:
        return PIPE_SDDL
    # Fallback (not used by default): allow all authenticated users full access.
    return "D:(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;AU)"


# --------------------------------------------------------------------------- #
# Low-level byte I/O over a pywin32 handle, with explicit-length framing.
# --------------------------------------------------------------------------- #
def _read_all(handle, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from a pipe handle; None on disconnect."""
    chunks = bytearray()
    while len(chunks) < n:
        try:
            _rc, data = win32file.ReadFile(handle, n - len(chunks))
        except pywintypes.error:
            return None  # client closed / pipe broken
        if not data:
            return None
        chunks.extend(data)
    return bytes(chunks)


def _write_all(handle, data: bytes) -> None:
    """Write all bytes to a pipe handle (single blocking WriteFile)."""
    win32file.WriteFile(handle, data)


# --------------------------------------------------------------------------- #
# Server side (Boss): create + accept the pipe, read frames in a loop.
# --------------------------------------------------------------------------- #
def create_server_handle():
    """Create the INBOUND named-pipe server handle with the ACL from the SDDL.

    Returns a pywin32 handle. The caller must call accept_client() next.
    """
    require_windows()
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        PIPE_SDDL, win32security.SDDL_REVISION_1
    )
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = True

    handle = win32pipe.CreateNamedPipe(
        PIPE_NAME,
        win32pipe.PIPE_ACCESS_INBOUND,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,  # one instance at a time
        _PIPE_BUFSIZE,  # out buffer (unused: inbound)
        proto.MAX_FRAME_SIZE + 64,  # in buffer: must hold one max frame
        0,  # default client wait timeout
        sa,
    )
    return handle


def accept_client(handle) -> bool:
    """Block until the Helper connects to the server pipe.

    Returns True once connected, False if the pipe errored before connect.
    """
    require_windows()
    try:
        win32pipe.ConnectNamedPipe(handle, None)
        return True
    except pywintypes.error:
        return False


def read_frame_blocking(handle) -> Optional[tuple]:
    """Read one framed packet from the pipe: uint32 size + payload + uint64 ts.

    Returns ``(payload, timestamp_ms)`` or ``None`` on disconnect. Runs in a
    worker thread inside the Boss (named pipes here are blocking, not asyncio).
    """
    size_buf = _read_all(handle, proto.SIZE_FIELD_SIZE)
    if size_buf is None:
        return None
    (frame_size,) = struct.unpack(">I", size_buf)
    if frame_size == 0 or frame_size > proto.MAX_FRAME_SIZE:
        raise ValueError(f"invalid frame size on pipe: {frame_size}")
    payload = _read_all(handle, frame_size)
    if payload is None:
        return None
    ts_buf = _read_all(handle, proto.TIMESTAMP_FIELD_SIZE)
    if ts_buf is None:
        return None
    (ts_ms,) = struct.unpack(">Q", ts_buf)
    return payload, ts_ms


def close_handle(handle) -> None:
    if handle is not None:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Client side (Helper): connect + a synchronous FrameSink that writes frames.
# --------------------------------------------------------------------------- #
def connect_client_handle():
    """Connect the Helper to the Boss's pipe. Returns a write handle."""
    require_windows()
    return win32file.CreateFile(
        PIPE_NAME,
        win32file.GENERIC_WRITE,
        0,  # no sharing
        None,  # default SECURITY_ATTRIBUTES
        win32file.OPEN_EXISTING,
        0,
        None,
    )


class PipeFrameWriter:
    """Synchronous FrameSink (matches daemon.capture.FrameSink) over the pipe.

    capture_loop calls sink.put(frame_bytes, timestamp_ms) without awaiting, so
    this writer must be synchronous -- it packs and writes in one shot.
    """

    def __init__(self, handle):
        self._handle = handle

    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None:
        _write_all(self._handle, proto.pack_frame(frame_bytes, timestamp_ms))

    def close(self) -> None:
        close_handle(self._handle)
        self._handle = None

