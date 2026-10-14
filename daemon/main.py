"""Daemon entry point + mode dispatcher.

Modes:
  * --foreground   : single process running capture + streamer together.
                     Used on Linux (dev/testing) and as the reference impl.
  * --worker       : capture-only, writes frames to a transport (Windows
                     per-user-session Helper). Implemented in Phase 2.
  * (no flag)      : run as the platform service (Windows Boss / Linux systemd
                     / macOS launchd). Implemented in Phase 2.

On Linux, the headless daemon is launched by systemd and daemonizes by
double-forking when invoked by the service manager (see service/linux.py).

CLI:  python -m daemon.main --config config.json --foreground
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Optional

from shared.config import Config

from daemon import capture as capture_mod
from daemon import streamer as streamer_mod


def _run_worker(cfg: Config) -> int:
    """Helper mode (Windows): capture the screen, write frames to the pipe.

    Runs in the interactive user's session with no window. Connects to the
    Boss's named-pipe server (retrying until it is up), then streams captured
    JPEG frames to it via the synchronous PipeFrameWriter sink.
    """
    import time

    from daemon import capture as capture_mod
    from daemon import pipe as pipe_mod

    # Connect to the Boss's pipe, retrying -- the Boss may still be starting.
    handle = None
    for _ in range(30):
        try:
            handle = pipe_mod.connect_client_handle()
            break
        except Exception:
            time.sleep(0.5)
    if handle is None:
        print("[worker] could not connect to Boss pipe; exiting", file=sys.stderr)
        return 3

    print("[worker] connected to Boss pipe; capturing", flush=True)
    sink = pipe_mod.PipeFrameWriter(handle)
    stop_event = asyncio.Event()
    cap_cfg = capture_mod.CaptureConfig(
        frame_rate=cfg.frame_rate, quality=cfg.quality, monitor=cfg.monitor
    )
    try:
        asyncio.run(capture_mod.capture_loop(cap_cfg, sink, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        sink.close()
    return 0



async def _run_foreground(cfg: Config, stop_event: asyncio.Event) -> None:
    """Single-process mode: capture -> queue -> streamer, all in one loop."""
    frame_queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.frame_rate * 2)
    source = streamer_mod.QueueFrameSource(frame_queue)
    cap_cfg = capture_mod.CaptureConfig(
        frame_rate=cfg.frame_rate, quality=cfg.quality, monitor=cfg.monitor
    )

    capture_task = asyncio.create_task(
        capture_mod.capture_loop(cap_cfg, capture_mod.QueueFrameSink(frame_queue), stop_event)
    )
    stream_task = asyncio.create_task(
        streamer_mod.stream_frames(
            cfg.viewer_ip,
            cfg.viewer_port,
            cfg.auth_token,
            cfg.tls_cert,
            source,
            stop_event,
        )
    )

    done, pending = await asyncio.wait(
        {capture_task, stream_task}, return_when=asyncio.FIRST_EXCEPTION
    )
    for task in pending:
        task.cancel()
    # Surface any exception from the completed task.
    for task in done:
        exc = task.exception()
        if exc is not None:
            raise exc


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="RemoteView daemon")
    parser.add_argument("--config", required=True, help="path to config.json")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="run capture+streamer in a single foreground process",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help="capture-only Helper mode: read the screen, write frames to the "
        "Boss's named pipe (Windows per-user session)",
    )
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)

    if args.worker:
        return _run_worker(cfg)

    stop_event = asyncio.Event()
    try:
        asyncio.run(_run_foreground(cfg, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
        print("\n[daemon] interrupted; shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
