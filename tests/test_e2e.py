"""End-to-end integration test: real capture -> TLS -> auth -> receiver.

Runs entirely in-process (no tkinter window, no separate OS processes).

NOTE: the streaming test uses mss, which needs an X display. It is skipped if
no DISPLAY is present. The auth-rejection test uses synthetic frames and runs
regardless.
"""

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import crypto
from daemon import capture as capture_mod
from daemon import streamer as streamer_mod
from viewer import receiver as receiver_mod


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """Starts a receiver TLS server on a background loop."""

    def __init__(self, port, cert, key, token_hex, out_q):
        self.port = port
        self.cert = cert
        self.key = key
        self.token_hex = token_hex
        self.out_q = out_q
        self.stop_event = asyncio.Event()
        self.thread = None

    def start(self, wait=0.8):
        def target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    receiver_mod.serve(
                        self.port, self.cert, self.key, self.token_hex,
                        self.out_q, self.stop_event,
                    )
                )
            except Exception:
                pass
            finally:
                loop.close()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        time.sleep(wait)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


class _Daemon:
    """Runs a streamer (and optional capture) on a background loop."""

    def __init__(self, port, cert, token_hex, frame_queue, with_capture=False,
                 cap_cfg=None):
        self.port = port
        self.cert = cert
        self.token_hex = token_hex
        self.frame_queue = frame_queue
        self.with_capture = with_capture
        self.cap_cfg = cap_cfg
        self.stop_event = asyncio.Event()
        self.thread = None

    async def _side(self):
        source = streamer_mod.QueueFrameSource(self.frame_queue)
        tasks = [
            asyncio.create_task(
                streamer_mod.stream_frames(
                    "127.0.0.1", self.port, self.token_hex, self.cert,
                    source, self.stop_event,
                )
            )
        ]
        if self.with_capture:
            tasks.append(
                asyncio.create_task(
                    capture_mod.capture_loop(
                        self.cap_cfg,
                        capture_mod.QueueFrameSink(self.frame_queue),
                        self.stop_event,
                    )
                )
            )
        await asyncio.sleep(self.run_seconds)
        self.stop_event.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    run_seconds = 3.0

    def start(self, run_seconds=None):
        if run_seconds is not None:
            self.run_seconds = run_seconds

        def target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._side())
            finally:
                loop.close()

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()

    def join(self, timeout=8):
        if self.thread:
            self.thread.join(timeout=timeout)


def _collect(out_q, min_frames, deadline_s):
    received = []
    deadline = time.time() + deadline_s
    while time.time() < deadline and len(received) < min_frames:
        try:
            received.append(out_q.get_nowait())
        except asyncio.QueueEmpty:
            time.sleep(0.05)
    return received


def test_auth_rejection_end_to_end():
    """A streamer with a WRONG token must not deliver any frames."""
    with tempfile.TemporaryDirectory() as d:
        cert = os.path.join(d, "server.crt")
        key = os.path.join(d, "server.key")
        crypto.generate_cert_and_key(cert, key, common_name="rv-e2e")
        good_token = crypto.generate_auth_token()
        bad_token = crypto.generate_auth_token()
        assert good_token != bad_token

        port = _free_port()
        out_q: asyncio.Queue = asyncio.Queue(maxsize=8)
        server = _Server(port, cert, key, good_token, out_q)
        server.start()

        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        daemon = _Daemon(port, cert, bad_token, frame_queue)
        daemon.start(run_seconds=2.0)

        # Push synthetic frames into the streamer's queue. The rejected
        # streamer will NOT consume them (auth fails first), so the queue may
        # fill -- tolerate QueueFull rather than crashing the feeder.
        async def feeder():
            for _ in range(10):
                try:
                    frame_queue.put_nowait((b"fakejpegdata", int(time.time() * 1000)))
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(0.05)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(feeder())
        loop.close()

        time.sleep(1.0)
        daemon.join(timeout=3)
        server.stop()

        assert out_q.empty(), "no frames should be delivered when auth fails"
        print("  ok: wrong-token streamer delivered 0 frames (auth enforced)")


def test_end_to_end_streaming():
    """Real mss capture -> TLS -> auth -> decoded frames at the receiver."""
    with tempfile.TemporaryDirectory() as d:
        cert = os.path.join(d, "server.crt")
        key = os.path.join(d, "server.key")
        crypto.generate_cert_and_key(cert, key, common_name="rv-e2e")
        token_hex = crypto.generate_auth_token()

        port = _free_port()
        out_q: asyncio.Queue = asyncio.Queue(maxsize=64)
        server = _Server(port, cert, key, token_hex, out_q)
        server.start()

        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        cap_cfg = capture_mod.CaptureConfig(frame_rate=8, quality=50, monitor=0)
        daemon = _Daemon(port, cert, token_hex, frame_queue,
                         with_capture=True, cap_cfg=cap_cfg)
        daemon.start(run_seconds=3.5)

        received = _collect(out_q, min_frames=5, deadline_s=6.0)
        daemon.join(timeout=8)
        server.stop()

        assert len(received) >= 1, "expected at least one frame end-to-end"
        img, ts_ms = received[-1]
        assert img.width > 0 and img.height > 0
        assert ts_ms > 0
        print(
            f"  ok: end-to-end {len(received)} frames; "
            f"last {img.width}x{img.height} @ {ts_ms}"
        )


def main():
    if not os.environ.get("DISPLAY") and sys.platform.startswith("linux"):
        print("No DISPLAY set; running auth test only (skipping mss capture).")
        test_auth_rejection_end_to_end()
        print("\nAuth test passed (capture skipped: no display).")
        return
    print("Running end-to-end streaming tests:")
    test_auth_rejection_end_to_end()
    test_end_to_end_streaming()
    print("\nAll end-to-end tests passed.")


if __name__ == "__main__":
    main()

