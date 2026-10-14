"""RemoteView wire protocol.

Frame packet (all integers big-endian), exactly as described in the design doc:

    +--------------+----------------------+-----------+
    | 4 bytes      | N bytes              | 8 bytes   |
    | frame_size   | JPEG/WebP payload    | unix_ms   |
    +--------------+----------------------+-----------+

    frame_size : uint32, byte length of the compressed image payload
    payload    : frame_size bytes (Pillow-compressed JPEG/WebP)
    unix_ms    : uint64, capture timestamp in milliseconds (latency tracking)

Reading order over the stream:
    1. read 4 bytes  -> frame_size
    2. read frame_size bytes -> payload
    3. read 8 bytes  -> unix_ms

Because TCP is a byte stream (no message boundaries), all reads must be
"read exactly N bytes" loops -- a single recv() may return fewer bytes than
requested. The helpers below guarantee that.

The auth handshake (performed over the TLS socket before any frame flows):

    Daemon  --[32-byte HMAC token]-->  Viewer
    Viewer  --[b"AUTH_OK" | b"AUTH_FAIL"]-->  Daemon
"""

from __future__ import annotations

import asyncio
import struct
from typing import BinaryIO, Optional, Tuple

# Size of the leading uint32 length prefix.
SIZE_FIELD_SIZE = 4
# Size of the trailing uint64 millisecond timestamp.
TIMESTAMP_FIELD_SIZE = 8

# The design doc refers to "HEADER_SIZE = 12 (4 + 8)": the combined size of the
# length prefix and the trailing timestamp. NOTE: the timestamp sits *after*
# the payload on the wire (per the doc's explicit read order), so it is not a
# single contiguous header block -- read_frame() handles the ordering.
HEADER_SIZE = SIZE_FIELD_SIZE + TIMESTAMP_FIELD_SIZE

# Wire format strings.
_SIZE_FMT = ">I"  # uint32 big-endian
_TS_FMT = ">Q"    # uint64 big-endian

# Hard safety cap for a single frame payload. Guards against a corrupt or
# hostile length prefix causing us to allocate/read gigabytes. 256 MiB is far
# above any realistic single-screen JPEG/WebP frame.
MAX_FRAME_SIZE = 256 * 1024 * 1024

# Auth handshake responses. The viewer sends one of these, newline-terminated,
# so the daemon can use readline() (robust to the different lengths below).
AUTH_OK = b"AUTH_OK"
AUTH_FAIL = b"AUTH_FAIL"
AUTH_NEWLINE = b"\n"
AUTH_RESPONSE_SIZE = max(len(AUTH_OK), len(AUTH_FAIL))

AUTH_TOKEN_SIZE = 32  # bytes

__all__ = [
    "SIZE_FIELD_SIZE",
    "TIMESTAMP_FIELD_SIZE",
    "HEADER_SIZE",
    "MAX_FRAME_SIZE",
    "AUTH_OK",
    "AUTH_FAIL",
    "AUTH_RESPONSE_SIZE",
    "AUTH_TOKEN_SIZE",
    "pack_frame",
    "unpack_frame_header",
    "unpack_frame",
    "read_exactly",
    "read_frame",
    "iter_frames",
    "aread_exactly",
    "aread_frame",
]


def pack_frame(image_bytes: bytes, timestamp_ms: int) -> bytes:
    """Serialize one frame to wire format.

    Layout: [uint32 frame_size][payload][uint64 unix_ms].
    """
    if timestamp_ms < 0:
        raise ValueError("timestamp_ms must be non-negative")
    if len(image_bytes) > MAX_FRAME_SIZE:
        raise ValueError(
            f"frame payload too large: {len(image_bytes)} > {MAX_FRAME_SIZE}"
        )
    return (
        struct.pack(_SIZE_FMT, len(image_bytes))
        + image_bytes
        + struct.pack(_TS_FMT, int(timestamp_ms))
    )


def unpack_frame_header(data: bytes) -> Tuple[int, int]:
    """Return ``(frame_size, timestamp_ms)`` for a complete frame buffer.

    Because the timestamp lives *after* the payload on the wire, this helper
    expects ``data`` to be a complete frame (size + payload + timestamp) as
    produced by :func:`pack_frame`, and validates that the declared length
    matches. Use :func:`read_frame` for streaming reads off a socket.
    """
    if len(data) < SIZE_FIELD_SIZE:
        raise ValueError("buffer too small to contain a size field")
    (frame_size,) = struct.unpack_from(_SIZE_FMT, data, 0)
    expected_total = SIZE_FIELD_SIZE + frame_size + TIMESTAMP_FIELD_SIZE
    if len(data) < expected_total:
        raise ValueError(
            f"incomplete frame: need {expected_total} bytes, got {len(data)}"
        )
    (timestamp_ms,) = struct.unpack_from(
        _TS_FMT, data, SIZE_FIELD_SIZE + frame_size
    )
    return frame_size, timestamp_ms


def unpack_frame(data: bytes) -> Tuple[bytes, int]:
    """Deserialize a complete frame buffer -> ``(payload, timestamp_ms)``."""
    frame_size, timestamp_ms = unpack_frame_header(data)
    payload = data[SIZE_FIELD_SIZE : SIZE_FIELD_SIZE + frame_size]
    return payload, timestamp_ms


def read_exactly(reader, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from ``reader`` (socket or file-like object).

    ``reader`` must expose either ``recv`` (socket / SSLSocket) or ``read``
    (file-like). Returns the bytes, or ``None`` if the stream closed before
    ``n`` bytes could be read (clean EOF / disconnect).
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    chunks = bytearray()
    remaining = n
    while remaining > 0:
        if hasattr(reader, "recv"):
            chunk = reader.recv(remaining)
        else:
            chunk = reader.read(remaining)
        if not chunk:
            # Stream closed mid-frame.
            return None
        chunks.extend(chunk)
        remaining -= len(chunk)
    return bytes(chunks)


def read_frame(reader) -> Optional[Tuple[bytes, int]]:
    """Read one complete frame from ``reader``.

    Returns ``(payload, timestamp_ms)`` or ``None`` on clean disconnect.
    Raises ``ValueError`` on a corrupt / oversized length prefix.
    """
    size_buf = read_exactly(reader, SIZE_FIELD_SIZE)
    if size_buf is None:
        return None
    (frame_size,) = struct.unpack(_SIZE_FMT, size_buf)
    if frame_size == 0 or frame_size > MAX_FRAME_SIZE:
        raise ValueError(f"invalid frame size: {frame_size}")
    payload = read_exactly(reader, frame_size)
    if payload is None:
        return None
    ts_buf = read_exactly(reader, TIMESTAMP_FIELD_SIZE)
    if ts_buf is None:
        return None
    (timestamp_ms,) = struct.unpack(_TS_FMT, ts_buf)
    return payload, timestamp_ms


def iter_frames(reader):
    """Yield ``(payload, timestamp_ms)`` frames until the stream ends."""
    while True:
        frame = read_frame(reader)
        if frame is None:
            return
        yield frame


# --------------------------------------------------------------------------- #
# Async variants for asyncio.StreamReader (used by the viewer receiver).
# asyncio StreamReader.read()/recv() are coroutines, so the synchronous helpers
# above cannot be used directly with them.
# --------------------------------------------------------------------------- #
async def aread_exactly(reader, n: int) -> Optional[bytes]:
    """Async read of exactly ``n`` bytes from an asyncio StreamReader.

    Returns the bytes, or ``None`` on clean/partial EOF.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return b""
    try:
        return await reader.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None


async def aread_frame(reader) -> Optional[Tuple[bytes, int]]:
    """Async read of one complete frame from an asyncio StreamReader.

    Returns ``(payload, timestamp_ms)`` or ``None`` on clean disconnect.
    Raises ``ValueError`` on a corrupt / oversized length prefix.
    """
    size_buf = await aread_exactly(reader, SIZE_FIELD_SIZE)
    if size_buf is None:
        return None
    (frame_size,) = struct.unpack(_SIZE_FMT, size_buf)
    if frame_size == 0 or frame_size > MAX_FRAME_SIZE:
        raise ValueError(f"invalid frame size: {frame_size}")
    payload = await aread_exactly(reader, frame_size)
    if payload is None:
        return None
    ts_buf = await aread_exactly(reader, TIMESTAMP_FIELD_SIZE)
    if ts_buf is None:
        return None
    (timestamp_ms,) = struct.unpack(_TS_FMT, ts_buf)
    return payload, timestamp_ms

