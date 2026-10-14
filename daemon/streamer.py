"""Frame streamer: connects out to the viewer, authenticates, sends frames.

Per the design doc's key design choice, the daemon always makes the *outbound*
connection to the viewer (which listens). This means no inbound firewall rules
are needed on the home machine.

The streamer is transport-agnostic on the frame side: it pulls frames from a
``FrameSource`` (an asyncio.Queue on Linux; a named-pipe reader inside the
Windows Boss). It owns the auth token + TLS and the reconnect logic.

Wire handshake (after TLS):
    Daemon  --[32-byte HMAC token]-->  Viewer
    Viewer  --[b"AUTH_OK" | b"AUTH_FAIL"]-->  Daemon
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Optional, Protocol

from shared import crypto
from shared import protocol as proto


class FrameSource(Protocol):
    """Yields ``(frame_bytes, timestamp_ms)`` tuples until stopped/closed."""

    async def get(self) -> Optional[tuple[bytes, int]]:
        """Return the next ``(payload, timestamp_ms)`` or None when the source
        is exhausted/closed."""
        ...


class QueueFrameSource:
    """FrameSource backed by an asyncio.Queue (Linux single-process mode)."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    async def get(self) -> Optional[tuple[bytes, int]]:
        return await self._queue.get()


# Reconnect backoff schedule: 1s, 2s, 4s, ... capped at 30s (per design doc).
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0


def _next_backoff(attempt: int) -> float:
    return min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** max(0, attempt - 1)))


async def _authenticate(reader, writer, auth_token_hex: str) -> bool:
    """Send the raw token, read AUTH_OK / AUTH_FAIL. Return True on AUTH_OK."""
    token_bytes = bytes.fromhex(auth_token_hex)
    await _send_all(writer, token_bytes)

    # The viewer replies with "AUTH_OK\n" or "AUTH_FAIL\n"; readline() handles
    # the variable response length robustly.
    try:
        resp = await asyncio.wait_for(reader.readline(), timeout=10.0)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        return False
    if not resp:
        return False
    return resp.strip() == proto.AUTH_OK


async def _send_all(writer_or_conn, data: bytes) -> None:
    """Send all bytes over an asyncio StreamWriter or socket-ish object."""
    if hasattr(writer_or_conn, "write") and hasattr(writer_or_conn, "drain"):
        writer_or_conn.write(data)
        await writer_or_conn.drain()
    else:  # raw socket (blocking) -- offload to thread
        await asyncio.get_event_loop().run_in_executor(
            None, writer_or_conn.sendall, data
        )


async def _recv_exactly(reader, n: int) -> Optional[bytes]:
    """Read exactly n bytes from an asyncio StreamReader."""
    if hasattr(reader, "readexactly"):
        try:
            return await reader.readexactly(n)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
    return None


async def stream_frames(
    viewer_ip: str,
    viewer_port: int,
    auth_token_hex: str,
    tls_cert_path: str,
    source: FrameSource,
    stop_event: Optional[asyncio.Event] = None,
    connect_timeout: float = 10.0,
) -> None:
    """Open TLS to the viewer, authenticate, and pump frames until stopped.

    Reconnects with exponential backoff (1s -> 2s -> 4s ... max 30s) whenever
    the connection drops, the handshake fails, or auth is rejected. Returns
    when ``stop_event`` is set.
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    ssl_ctx = crypto.build_ssl_context_client(tls_cert_path)
    attempt = 0

    while not stop_event.is_set():
        writer: Optional[asyncio.StreamWriter] = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    viewer_ip, viewer_port, ssl=ssl_ctx, server_hostname=None
                ),
                timeout=connect_timeout,
            )
        except (asyncio.TimeoutError, OSError, ssl.SSLError) as exc:
            attempt += 1
            delay = _next_backoff(attempt)
            _log(f"connect failed ({exc!r}); retry in {delay:.0f}s")
            await _sleep_or_stop(stop_event, delay)
            continue

        # Authenticate over the established TLS channel.
        try:
            ok = await _authenticate(reader, writer, auth_token_hex)
        except (ConnectionError, ssl.SSLError, OSError) as exc:
            ok = False
            _log(f"auth transport error: {exc!r}")

        if not ok:
            _log("auth rejected (AUTH_FAIL); closing and retrying")
            await _safe_close(writer)
            attempt += 1
            delay = _next_backoff(attempt)
            await _sleep_or_stop(stop_event, delay)
            continue

        # Connected + authenticated. Reset backoff.
        attempt = 0
        _log("authenticated; streaming frames")

        try:
            while not stop_event.is_set():
                # Poll with a timeout so stop_event can interrupt an idle
                # queue (otherwise a blocked get() would only end on cancel).
                try:
                    item = await asyncio.wait_for(source.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    # Source closed -> treat like disconnect, small backoff.
                    await _sleep_or_stop(stop_event, 0.5)
                    break
                frame_bytes, ts_ms = item
                packet = proto.pack_frame(frame_bytes, ts_ms)
                await _send_all(writer, packet)
        except (ConnectionError, ssl.SSLError, OSError) as exc:
            _log(f"connection lost: {exc!r}; reconnecting")
        finally:
            await _safe_close(writer)


async def _safe_close(writer: Optional[asyncio.StreamWriter]) -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass


async def _sleep_or_stop(stop_event: asyncio.Event, delay: float) -> None:
    """Sleep for ``delay`` seconds, returning early if stop_event is set."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


def _log(msg: str) -> None:
    # Kept dependency-free; the service layer can redirect logging later.
    print(f"[streamer] {msg}", flush=True)
