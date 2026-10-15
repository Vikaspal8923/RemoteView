"""TLS receiver: listens for the daemon, authenticates, decodes frames.

Runs as an asyncio task in a background thread (the main thread is reserved
for tkinter). Responsibilities:

  * Open a TLS 1.3 listener on 0.0.0.0:viewer_port, pinning the self-signed
    cert (the server presents the pinned cert).
  * Accept one connection at a time (per the design doc).
  * Verify the 32-byte auth token (constant-time) BEFORE any frame flows.
  * Send b"AUTH_OK" / b"AUTH_FAIL".
  * Read frame packets in a loop, decode JPEG -> PIL.Image, and push into an
    asyncio.Queue that the display reads from.
"""

from __future__ import annotations

import asyncio
import io
import ssl
import queue
from typing import Optional

from PIL import Image

from shared import crypto
from shared import protocol as proto


class AuthError(Exception):
    """Raised when a client fails the token check."""


async def _handle_client(reader, writer, expected_token_hex: str, out_queue: asyncio.Queue, control_queue=None):
    """Handle one daemon connection: auth, then stream frames into out_queue."""
    peer = writer.get_extra_info("peername")
    try:
        # 1. Read the 32-byte auth token.
        token = await _recv_exactly(reader, proto.AUTH_TOKEN_SIZE)
        if token is None:
            return
        if not crypto.verify_token(token, expected_token_hex):
            await _send_all(writer, proto.AUTH_FAIL + proto.AUTH_NEWLINE)
            print(f"[receiver] auth FAIL from {peer}", flush=True)
            return
        await _send_all(writer, proto.AUTH_OK + proto.AUTH_NEWLINE)
        print(f"[receiver] auth OK from {peer}; streaming", flush=True)

        async def send_controls():
            if control_queue is None:
                return
            while True:
                try:
                    msg = await asyncio.to_thread(control_queue.get, True, 0.2)
                    writer.write(msg)
                    await writer.drain()
                except queue.Empty:
                    continue
                except (ConnectionError, OSError, asyncio.CancelledError):
                    return

        control_task = asyncio.create_task(send_controls()) if control_queue is not None else None

        # 2. Read frames until the client disconnects.
        while True:
            item = await proto.aread_frame(reader)
            if item is None:
                print(f"[receiver] client {peer} disconnected", flush=True)
                return
            payload, ts_ms = item
            try:
                img = Image.open(io.BytesIO(payload))
                img.load()  # force decode now so the payload buffer can be freed
            except Exception as exc:
                print(f"[receiver] bad frame from {peer}: {exc!r}", flush=True)
                continue
            # Bounded queue: drop oldest if the display can't keep up.
            if out_queue.full():
                try:
                    out_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await out_queue.put((img, ts_ms))
    except (ConnectionError, ssl.SSLError, OSError) as exc:
        print(f"[receiver] connection error with {peer}: {exc!r}", flush=True)
    finally:
        if 'control_task' in locals() and control_task is not None:
            control_task.cancel()
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass


async def serve(
    viewer_port: int,
    tls_cert_path: str,
    tls_key_path: str,
    expected_token_hex: str,
    out_queue: asyncio.Queue,
    stop_event: Optional[asyncio.Event] = None,
    control_queue=None,
) -> None:
    """Listen for daemons, authenticate, and fill ``out_queue`` with frames.

    Runs until ``stop_event`` is set.
    """
    if stop_event is None:
        stop_event = asyncio.Event()

    ssl_ctx = crypto.build_ssl_context_server(tls_cert_path, tls_key_path)
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, expected_token_hex, out_queue, control_queue),
        host="0.0.0.0",
        port=viewer_port,
        ssl=ssl_ctx,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    print(f"[receiver] listening on {addrs} (TLS 1.3)", flush=True)

    async with server:
        await stop_event.wait()


async def _send_all(writer, data: bytes) -> None:
    writer.write(data)
    await writer.drain()


async def _recv_exactly(reader, n: int) -> Optional[bytes]:
    try:
        return await reader.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
