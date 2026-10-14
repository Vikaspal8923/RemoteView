"""Windows Service wrapper for the RemoteView daemon (the "Boss").

Runs as a registered Windows Service under LocalSystem in Session 0. It:

  1. Starts a background thread that supervises the capture Helper (launched
     into the interactive user's session; respawned on death).
  2. Creates the named-pipe frame server and reads JPEG frames from the Helper.
  3. Bridges those frames into the asyncio streamer, which owns the auth token
     + TLS and dials out to the viewer.

Because it is a LocalSystem service, a normal (non-admin) user process cannot
stop it -- stopping requires admin rights (services.msc, `sc stop`, Task
Manager Services tab). The service has no window.

The pywin32 service base class must be built at import time on Windows, so the
real class is assembled by build_service_class(). This module imports cleanly
on Linux so the rest of the package (and tests) can load it.

Manage via the dispatcher at the bottom:
    python -m daemon.service.windows install|start|stop|remove
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Optional

SERVICE_NAME = "RemoteViewDaemon"
DISPLAY_NAME = "RemoteView Daemon"


def _load_win32service():
    if not sys.platform.startswith("win"):
        raise RuntimeError("the Windows service is Windows-only")
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore
    return servicemanager, win32event, win32service, win32serviceutil


class RemoteViewBossService:
    """Boss service implementation (pywin32 mixin injected on Windows).

    On Windows, build_service_class() returns a subclass that also inherits the
    pywin32 service base + provides ReportServiceStatus/SvcDoRun plumbing. This
    class holds the portable orchestration logic.
    """

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = "RemoteView screen monitoring daemon (un-killable Boss)."

    def __init__(self, args):
        self._stop_event = threading.Event()
        self._config_path = os.environ.get("REMOTEVIEW_CONFIG", "config.json")
        self._threads = []

    # ---- pywin32 service lifecycle ---- #
    def SvcStop(self):
        """Called by the SCM on stop request."""
        self.ReportServiceStatus(self._win32service.SERVICE_STOP_PENDING)
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5)

    def SvcDoRun(self):
        """Main service entry point (called by the SCM after Start)."""
        import servicemanager

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._stop_event.clear()
        self.main()

    # ---- Boss orchestration ---- #
    def main(self):
        from shared.config import Config
        from daemon import pipe as pipe_mod
        from daemon import streamer as streamer_mod
        from daemon import supervisor as supervisor_mod

        cfg = Config.load(self._config_path)
        stop_event = self._stop_event

        # 1. Supervisor thread: keep the Helper alive in the user session.
        sup_thread = threading.Thread(
            target=supervisor_mod.supervise,
            args=(cfg, lambda: not stop_event.is_set()),
            name="helper-supervisor",
            daemon=True,
        )
        sup_thread.start()
        self._threads.append(sup_thread)

        # 2. Frame pump: create the pipe server, accept the Helper, and push
        #    frames into an asyncio.Queue consumed by the streamer.
        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=max(4, cfg.frame_rate * 2))

        # 3. Streamer: owns TLS + auth, dials out to the viewer. Runs here.
        source = _AsyncQueueSource(frame_queue, stop_event)

        async def run_stream():
            await streamer_mod.stream_frames(
                cfg.viewer_ip,
                cfg.viewer_port,
                cfg.auth_token,
                cfg.tls_cert,
                source,
                asyncio.Event(),  # streamer manages its own reconnect loop
            )

        # The pipe pump runs on the same asyncio loop as the streamer via
        # run_in_executor (blocking named-pipe reads), so no cross-thread queue
        # juggling is needed.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_until_stop(loop, frame_queue, run_stream()))
        finally:
            loop.close()

    async def _run_until_stop(self, loop, frame_queue, stream_coro):
        """Run the pipe pump + streamer together until stop_event is set."""
        pump_task = asyncio.ensure_future(self._pipe_pump(loop, frame_queue))
        stream_task = asyncio.ensure_future(stream_coro)
        while not self._stop_event.is_set():
            if stream_task.done():
                break
            await asyncio.sleep(0.25)
        pump_task.cancel()
        stream_task.cancel()
        for t in (pump_task, stream_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def _pipe_pump(self, loop, frame_queue: asyncio.Queue):
        """Create + accept the named pipe; read frames into frame_queue.

        Blocking named-pipe I/O is offloaded to a thread executor so it does not
        stall the asyncio loop that drives the TLS streamer.
        """
        from daemon import pipe as pipe_mod

        while not self._stop_event.is_set():
            handle = await loop.run_in_executor(None, pipe_mod.create_server_handle)
            connected = await loop.run_in_executor(None, pipe_mod.accept_client, handle)
            if not connected:
                pipe_mod.close_handle(handle)
                await asyncio.sleep(0.5)
                continue
            print("[boss] Helper connected; reading frames", flush=True)
            while not self._stop_event.is_set():
                item = await loop.run_in_executor(
                    None, pipe_mod.read_frame_blocking, handle
                )
                if item is None:
                    print("[boss] Helper disconnected", flush=True)
                    break
                payload, ts_ms = item
                if frame_queue.full():
                    try:
                        frame_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                frame_queue.put_nowait((payload, ts_ms))
            pipe_mod.close_handle(handle)


class _AsyncQueueSource:
    """FrameSource bridging the pipe pump's asyncio.Queue to the streamer.

    Interruptible get() so the stream loop ends promptly on stop.
    """

    def __init__(self, queue: asyncio.Queue, stop_event: threading.Event):
        self._queue = queue
        self._stop = stop_event

    async def get(self):
        while not self._stop.is_set():
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
        return None


def build_service_class():
    """Assemble the concrete pywin32 service class (Windows only).

    Mixes RemoteViewBossService with pywin32's win32serviceutil.ServiceFramework
    and injects the win32service module reference used by SvcStop.
    """
    _servicemanager, _win32event, win32service, win32serviceutil = _load_win32service()

    class RemoteViewWindowsService(RemoteViewBossService, win32serviceutil.ServiceFramework):
        _win32service = win32service

        def SvcDoRun(self):
            # ServiceFramework provides the SCM event handle plumbing.
            self._stop_handle = _win32event.CreateEvent(None, 0, 0, None)
            super().SvcDoRun()

        def SvcStop(self):
            # Signal our stop event, then let the SCM know we're stopping.
            if hasattr(self, "ReportServiceStatus"):
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop_event.set()

    return RemoteViewWindowsService



if __name__ == "__main__":
    # CLI dispatcher: install / start / stop / remove / debug.
    # Usage: python -m daemon.service.windows <command>
    if not sys.platform.startswith("win"):
        print("daemon.service.windows is Windows-only.", file=sys.stderr)
        raise SystemExit(2)

    _sm, _we, _ws, win32serviceutil = _load_win32service()
    RemoteViewWindowsService = build_service_class()

    if len(sys.argv) == 1:
        # Started by the SCM: hand control to pywin32's service dispatcher.
        _sm.Initialize()
        _sm.PrepareToHostSingleService(RemoteViewWindowsService)
        _sm.StartServiceCtrlDispatcher()
    else:
        # Management command from an admin shell.
        win32serviceutil.HandleCommandLine(RemoteViewWindowsService)

