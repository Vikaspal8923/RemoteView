"""Viewer entry point.

CLI:  python -m viewer.main --config config.json

Starts the TLS receiver as an asyncio loop on a background thread and runs the
tkinter display on the main thread. Frames flow:
    receiver (asyncio) -> thread-safe bridge queue -> display (tkinter).
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import threading
from typing import Optional

from shared.config import Config

from viewer import receiver as receiver_mod
from viewer import display as display_mod

# Bounded bridge queue: bounds memory + lets us drop stale frames under load.
BRIDGE_QUEUE_MAX = 4


def _run_receiver_thread(
    cfg: Config,
    bridge: "queue.Queue",
    stop_event: asyncio.Event,
    ready: threading.Event,
    control_queue,
) -> threading.Thread:
    """Start the asyncio receiver loop on a daemon thread."""

    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def pump():
            # Move frames from the asyncio out_queue to the thread-safe bridge.
            out_q: asyncio.Queue = asyncio.Queue(maxsize=BRIDGE_QUEUE_MAX)
            serve_task = loop.create_task(
                receiver_mod.serve(
                    cfg.viewer_port,
                    cfg.tls_cert,
                    cfg.tls_key,
                    cfg.auth_token,
                    out_q,
                    stop_event,
                    control_queue,
                )
            )
            ready.set()
            while not stop_event.is_set():
                try:
                    item = await asyncio.wait_for(out_q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                # Drop oldest if bridge is full.
                if bridge.full():
                    try:
                        bridge.get_nowait()
                    except queue.Empty:
                        pass
                bridge.put(item)

        try:
            loop.run_until_complete(pump())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    t = threading.Thread(target=target, name="receiver", daemon=True)
    t.start()
    return t


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteView viewer")
    parser.add_argument("--config", required=True, help="path to config.json")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)

    bridge: "queue.Queue" = queue.Queue(maxsize=BRIDGE_QUEUE_MAX)
    control_queue = queue.Queue()
    stop_event = asyncio.Event()
    ready = threading.Event()

    _run_receiver_thread(cfg, bridge, stop_event, ready, control_queue)
    ready.wait(timeout=5.0)

    # tkinter on the main thread; returns when the window is closed / Q pressed.
    def send_control(kind, **data):
        from shared.control import make_control
        control_queue.put(make_control(kind, **data))

    disp = display_mod.ViewerDisplay(bridge, title="RemoteView", control_send=send_control)
    disp.run()

    # Signal the receiver thread to stop.
    stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
