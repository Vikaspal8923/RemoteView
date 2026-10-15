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
import time
from typing import Optional

from shared.config import Config

from daemon import capture as capture_mod
from daemon import streamer as streamer_mod


def _run_worker(cfg: Config) -> int:
    """Helper mode: capture the screen, write frames to the Boss's socket.

    Runs in the interactive user's session with no window. Connects to the
    Boss's loopback socket (retrying until it is up), authenticates with the
    shared token, then streams captured JPEG frames to it via the synchronous
    FrameSocketWriter sink.
    """
    import time
    def worker_log(message):
        try:
            import os
            path = os.path.join(os.path.dirname(os.path.abspath(cfg.config_path or "config.json")), "remoteview-helper.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except Exception:
            pass
        print(message, flush=True)

    from daemon import capture as capture_mod
    from daemon import transport as transport_mod

    # Connect to the Boss's socket, retrying -- the Boss may still be starting.
    sock = None
    for _ in range(30):
        try:
            sock = transport_mod.connect_client()
            break
        except OSError:
            time.sleep(0.5)
    if sock is None:
        print("[worker] could not connect to Boss socket; exiting", file=sys.stderr)
        return 3

    # Authenticate before streaming any frames.
    if not transport_mod.client_authenticate(sock, cfg.auth_token):
        print("[worker] Boss rejected our token; exiting", file=sys.stderr)
        transport_mod.close_handle(sock)
        return 4

    worker_log("[worker] connected + authenticated to Boss; capturing")
    import threading
    from shared.control import decode_control_message
    from daemon import input as input_mod
    from daemon.input_arbiter import LocalInputArbiter
    # Testing mode: accept input immediately after the authenticated Helper
    # connection. Escape/F8 on the viewer can disable it.
    control_enabled = True
    arbiter = LocalInputArbiter(idle_seconds=1.5)
    arbiter.start()
    last_local_pause_log = 0.0
    def read_controls():
        nonlocal control_enabled, last_local_pause_log
        try:
            reader = sock.makefile("rb")
            while True:
                line = reader.readline()
                if not line:
                    return
                try:
                    msg = decode_control_message(line)
                    if msg.type == "CONTROL_ENABLE":
                        control_enabled = True
                        worker_log("[worker] remote control enabled")
                    elif msg.type in {"CONTROL_DISABLE", "EMERGENCY_STOP"}:
                        control_enabled = False; input_mod.emergency_release()
                        worker_log("[worker] remote control disabled")
                    elif control_enabled:
                        if not arbiter.remote_allowed():
                            input_mod.emergency_release()
                            now = time.monotonic()
                            if now - last_local_pause_log > 1.0:
                                worker_log("[worker] remote input paused: local user active")
                                last_local_pause_log = now
                            continue
                        d = msg.data
                        if msg.type == "MOUSE_MOVE": input_mod.move_normalized(d["x"], d["y"])
                        elif msg.type.startswith("MOUSE_BUTTON_"): input_mod.mouse_button(d["button"], msg.type.endswith("DOWN"))
                        elif msg.type == "MOUSE_SCROLL": input_mod.scroll(d["delta"])
                        elif msg.type in {"KEY_DOWN", "KEY_UP"}: input_mod.key_name(d["key"], msg.type.endswith("DOWN"))
                        elif msg.type == "TYPE_TEXT": input_mod.type_text(d["text"])
                except Exception as exc:
                    worker_log(f"[worker] control rejected: {exc!r}")
        finally:
            control_enabled = False
            input_mod.emergency_release()
    threading.Thread(target=read_controls, daemon=True, name="control-reader").start()
    sink = transport_mod.FrameSocketWriter(sock)
    stop_event = asyncio.Event()
    cap_cfg = capture_mod.CaptureConfig(
        frame_rate=cfg.frame_rate, quality=cfg.quality, monitor=cfg.monitor,
        max_width=cfg.max_width, max_height=cfg.max_height,
        capture_backend=cfg.capture_backend, affinity_enabled=cfg.affinity_enabled,
        affinity_poll_ms=cfg.affinity_poll_ms, affinity_max_failures=cfg.affinity_max_failures,
    )
    try:
        asyncio.run(capture_mod.capture_loop(cap_cfg, sink, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        arbiter.stop()
        sink.close()
    return 0



async def _run_foreground(cfg: Config, stop_event: asyncio.Event) -> None:
    """Single-process mode: capture -> queue -> streamer, all in one loop."""
    frame_queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.frame_rate * 2)
    source = streamer_mod.QueueFrameSource(frame_queue)
    cap_cfg = capture_mod.CaptureConfig(
        frame_rate=cfg.frame_rate, quality=cfg.quality, monitor=cfg.monitor,
        max_width=cfg.max_width, max_height=cfg.max_height,
        capture_backend=cfg.capture_backend, affinity_enabled=cfg.affinity_enabled,
        affinity_poll_ms=cfg.affinity_poll_ms, affinity_max_failures=cfg.affinity_max_failures,
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
        help="capture-only Helper mode: read the screen, stream frames to the "
        "Boss's loopback socket (runs in the user session)",
    )
    parser.add_argument("--diagnose-windows", action="store_true",
                        help="print visible top-level window affinity diagnostics")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--capture-once", action="store_true")
    parser.add_argument("--dump")
    parser.add_argument("--fake-reader", action="store_true")
    parser.add_argument("--pretend-wda", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)

    if args.probe:
        from daemon.capture.win32.api import user32, d3d11, dxgi
        import json
        data = {"platform": sys.platform, "exports": {"user32": user32 is not None, "d3d11": d3d11 is not None, "dxgi": dxgi is not None}}
        print(json.dumps(data) if args.json_output else data)
        return 0

    if args.diagnose_windows:
        from daemon.capture.affinity import scan
        print("HWND\tCLASS\tTITLE\tCLIENT_RECT\tZ_INDEX\tAFFINITY")
        for w in scan():
            r = w.client_rect
            print(f"{w.hwnd}\t{w.class_name}\t{w.title}\t{r.left},{r.top},{r.right},{r.bottom}\t{w.z_index}\t{w.affinity}")
        return 0

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
