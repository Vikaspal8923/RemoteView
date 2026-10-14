"""Local frame transport between the Boss and the Helper (same machine).

Why a socket instead of a named pipe? Named-pipe creation on Windows is highly
sensitive to security descriptors / session context and repeatedly failed with
ERROR_ACCESS_DENIED on the target machine. A loopback TCP socket on 127.0.0.1
does the identical job -- passing captured frames from the Helper (user session)
to the Boss (Session-0 service) -- with no ACL complications, and it is plain
Python so it is unit-testable on Linux too.

Direction:
    Helper (user session)  --connect-->  Boss (service)  --reads frames-->
The Helper authenticates to the Boss with the same 32-byte auth token used for
the viewer TLS handshake (constant-time verify), then streams frames. This
prevents a rogue local process from injecting frames.

Frame format reuses shared.protocol.pack_frame (uint32 size + JPEG payload +
uint64 unix_ms), so the wire format is identical end to end.

Security note: the socket binds to 127.0.0.1 only (loopback), so it is not
reachable from the network. The real security boundary remains the TLS +
auth-token layer inside the Boss when it streams to the remote viewer; the
token and private key are never sent over this local socket. A local admin/root
process could still sniff loopback frames -- the same trust model as any local
IPC -- which is acceptable for this self-hosted, single-machine v1.
"""

from __future__ import annotations

import socket
import struct
from typing import Optional

from shared import crypto
from shared import protocol as proto

# Fixed loopback port for the Helper -> Boss frame channel. Both processes run
# on the same machine and agree on this constant. Overridable for tests.
LOCAL_FRAME_PORT = 47921
LOCAL_HOST = "127.0.0.1"

# Socket timeouts (seconds).
_ACCEPT_TIMEOUT = 1.0       # Boss: how long accept() blocks before re-checking stop
_CONNECT_TIMEOUT = 10.0     # Helper: how long to wait for the Boss to be listening

__all__ = [
    "LOCAL_FRAME_PORT",
    "LOCAL_HOST",
    "create_server",
    "accept_client",
    "server_authenticate",
    "read_frame_blocking",
    "close_handle",
    "connect_client",
    "client_authenticate",
    "FrameSocketWriter",
]


# --------------------------------------------------------------------------- #
# Low-level framed byte I/O over a socket.
# --------------------------------------------------------------------------- #
def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from a socket; None on disconnect."""
    chunks = bytearray()
    while len(chunks) < n:
        try:
            data = sock.recv(n - len(chunks))
        except (socket.timeout, OSError):
            return None
        if not data:
            return None  # peer closed
        chunks.extend(data)
    return bytes(chunks)


# --------------------------------------------------------------------------- #
# Server side (Boss, Session 0): listen, accept, authenticate, read frames.
# --------------------------------------------------------------------------- #
def create_server(port: int = LOCAL_FRAME_PORT, host: str = LOCAL_HOST) -> socket.socket:
    """Create + bind + listen on a loopback TCP socket. Returns the listen socket."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    srv.settimeout(_ACCEPT_TIMEOUT)
    return srv


def accept_client(listen_sock: socket.socket) -> Optional[socket.socket]:
    """Accept one client connection.

    Returns the connected socket, or None on timeout / error (the caller should
    retry). The returned socket is set to blocking with no timeout so the frame
    read loop blocks naturally.
    """
    try:
        conn, _addr = listen_sock.accept()
    except (socket.timeout, OSError):
        return None
    conn.settimeout(None)
    return conn


def server_authenticate(conn: socket.socket, expected_token_hex: str) -> bool:
    """Read the Helper's 32-byte token and verify it; reply AUTH_OK / AUTH_FAIL."""
    token = _recv_exact(conn, proto.AUTH_TOKEN_SIZE)
    if token is None:
        return False
    if not crypto.verify_token(token, expected_token_hex):
        try:
            conn.sendall(proto.AUTH_FAIL + proto.AUTH_NEWLINE)
        except OSError:
            pass
        return False
    try:
        conn.sendall(proto.AUTH_OK + proto.AUTH_NEWLINE)
    except OSError:
        return False
    return True


def read_frame_blocking(conn: socket.socket) -> Optional[tuple]:
    """Read one framed packet from the socket: uint32 size + payload + uint64 ts.

    Returns ``(payload, timestamp_ms)`` or ``None`` on disconnect. Runs in a
    worker thread inside the Boss (blocking socket reads).
    """
    size_buf = _recv_exact(conn, proto.SIZE_FIELD_SIZE)
    if size_buf is None:
        return None
    (frame_size,) = struct.unpack(">I", size_buf)
    if frame_size == 0 or frame_size > proto.MAX_FRAME_SIZE:
        raise ValueError(f"invalid frame size on socket: {frame_size}")
    payload = _recv_exact(conn, frame_size)
    if payload is None:
        return None
    ts_buf = _recv_exact(conn, proto.TIMESTAMP_FIELD_SIZE)
    if ts_buf is None:
        return None
    (ts_ms,) = struct.unpack(">Q", ts_buf)
    return payload, ts_ms


def close_handle(sock: Optional[socket.socket]) -> None:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass



# --------------------------------------------------------------------------- #
# Client side (Helper, user session): connect, authenticate, write frames.
# --------------------------------------------------------------------------- #
def connect_client(
    port: int = LOCAL_FRAME_PORT, host: str = LOCAL_HOST, timeout: float = _CONNECT_TIMEOUT
) -> socket.socket:
    """Connect the Helper to the Boss's loopback socket. Returns the socket."""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(None)
    return sock


def client_authenticate(sock: socket.socket, auth_token_hex: str) -> bool:
    """Send the 32-byte token, read AUTH_OK / AUTH_FAIL. Return True on AUTH_OK."""
    try:
        sock.sendall(bytes.fromhex(auth_token_hex))
    except OSError:
        return False
    resp = _recv_exact(sock, len(proto.AUTH_OK) + len(proto.AUTH_NEWLINE))
    if resp is None:
        return False
    return resp.strip() == proto.AUTH_OK


class FrameSocketWriter:
    """Synchronous FrameSink (matches daemon.capture.FrameSink) over the socket.

    capture_loop calls sink.put(frame_bytes, timestamp_ms) without awaiting, so
    this writer must be synchronous -- it packs and sends in one shot.
    """

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def put(self, frame_bytes: bytes, timestamp_ms: int) -> None:
        self._sock.sendall(proto.pack_frame(frame_bytes, timestamp_ms))

    def close(self) -> None:
        close_handle(self._sock)
        self._sock = None

