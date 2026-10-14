"""Windows Service wrapper for the RemoteView daemon (the "Boss").

Runs as a registered Windows Service under LocalSystem in Session 0. It:
  1. supervises the capture Helper (launched into the user's session,
     respawned on death),
  2. accepts the Helper on a loopback socket and reads JPEG frames from it,
  3. bridges those frames into the asyncio streamer, which owns the auth token
     + TLS and dials out to the viewer.

Because it is a LocalSystem service, a normal (non-admin) user process cannot
stop it -- stopping requires admin rights (services.msc, `sc stop`, Task
Manager Services tab). The service has no window.

The pywin32 ServiceFramework requires a specific lifecycle: __init__ MUST call
ServiceFramework.__init__(self, args) and create a stop event; SvcStop signals
that event; SvcDoRun runs the work until the event fires. This module imports
cleanly on Linux; the concrete class is built by build_service_class().

Manage: python -m daemon.service.windows install|start|stop|remove|debug
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Optional

SERVICE_NAME = "RemoteViewDaemon"
DISPLAY_NAME = "RemoteView Daemon"
SERVICE_DESCRIPTION = "RemoteView screen monitoring daemon (un-killable Boss)."


def _load_win32service():
    if not sys.platform.startswith("win"):
        raise RuntimeError("the Windows service is Windows-only")
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore
    return servicemanager, win32event, win32service, win32serviceutil


class BossOrchestrator:
    """Portable Boss orchestration logic (mixed into the pywin32 service).

    Holds everything that is not pywin32-specific. The pywin32 lifecycle
    methods live on the concrete class built by build_service_class().
    """

    def __init__(self, config_path: str):
        self._config_path = config_path
        self._stop = threading.Event()  # set by SvcStop / shutdown
        self._threads = []

    def run(self, poll_seconds: float = 0.5) -> None:
        """Run the Boss until stop() is called. Blocks."""
        from shared.config import Config
        from daemon import supervisor as supervisor_mod

        cfg = Config.load(self._config_path)

        # 1. Supervisor thread: keep the Helper alive in the user session.
        sup_thread = threading.Thread(
            target=supervisor_mod.supervise,
            args=(cfg, lambda: not self._stop.is_set()),
            name="helper-supervisor",
            daemon=True,
        )
        sup_thread.start()
        self._threads.append(sup_thread)

        # 2. Frame pump + 3. streamer run together on one asyncio loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=max(4, cfg.frame_rate * 2))
        source = _AsyncQueueSource(frame_queue, self._stop)
        try:
            loop.run_until_complete(self._run_until_stop(loop, frame_queue, source, cfg))
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def _run_until_stop(self, loop, frame_queue, source, cfg):
        from daemon import streamer as streamer_mod

        pump_task = asyncio.ensure_future(self._frame_pump(loop, frame_queue, cfg))
        stream_task = asyncio.ensure_future(
            streamer_mod.stream_frames(
                cfg.viewer_ip,
                cfg.viewer_port,
                cfg.auth_token,
                cfg.tls_cert,
                source,
                asyncio.Event(),  # streamer manages its own reconnect loop
            )
        )
        # Poll the stop flag; also exit if the stream task ends unexpectedly.
        while not self._stop.is_set() and not stream_task.done():
            await asyncio.sleep(0.25)
        pump_task.cancel()
        stream_task.cancel()
        for t in (pump_task, stream_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def _frame_pump(self, loop, frame_queue: asyncio.Queue, cfg):
        """Create + accept the loopback socket; read frames into frame_queue.

        Blocking socket I/O is offloaded to a thread executor so it does not
        stall the asyncio loop driving the TLS streamer. The Helper must present
        the shared auth token before any frames are accepted.
        """
        from daemon import transport as transport_mod

        while not self._stop.is_set():
            listen_sock = await loop.run_in_executor(None, transport_mod.create_server)
            conn = await loop.run_in_executor(None, transport_mod.accept_client, listen_sock)
            if conn is None:
                transport_mod.close_handle(listen_sock)
                await asyncio.sleep(0.25)
                continue
            # Authenticate the Helper before reading any frames.
            authed = await loop.run_in_executor(
                None, transport_mod.server_authenticate, conn, cfg.auth_token
            )
            if not authed:
                print("[boss] Helper failed auth; dropping", flush=True)
                transport_mod.close_handle(conn)
                transport_mod.close_handle(listen_sock)
                await asyncio.sleep(0.25)
                continue
            print("[boss] Helper connected + authenticated; reading frames", flush=True)
            while not self._stop.is_set():
                item = await loop.run_in_executor(
                    None, transport_mod.read_frame_blocking, conn
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
            transport_mod.close_handle(conn)
            transport_mod.close_handle(listen_sock)


class _AsyncQueueSource:
    """FrameSource bridging the pipe pump's asyncio.Queue to the streamer."""

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

    Mixes BossOrchestrator with pywin32's ServiceFramework and implements the
    required lifecycle (__init__ / SvcStop / SvcDoRun).
    """
    servicemanager, win32event, win32service, win32serviceutil = _load_win32service()

    class RemoteViewWindowsService(win32serviceutil.ServiceFramework, BossOrchestrator):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args):
            # REQUIRED: initialize the pywin32 service framework first.
            win32serviceutil.ServiceFramework.__init__(self, args)
            # Stop event signalled by SvcStop; SvcDoRun waits on it.
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            config_path = os.environ.get("REMOTEVIEW_CONFIG", "config.json")
            BossOrchestrator.__init__(self, config_path)

        def SvcStop(self):
            # Tell the SCM we are stopping, then signal our stop event.
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)
            self.stop()

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            # Run the Boss work on a daemon thread so we can wait on hWaitStop
            # here (the canonical pattern) and return promptly on stop.
            work = threading.Thread(target=self.run, name="boss-run", daemon=True)
            work.start()
            self._threads.append(work)
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
            self.stop()
            work.join(timeout=10)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, ""),
            )

    return RemoteViewWindowsService


if __name__ == "__main__":
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
        # Management command from an admin shell (install/start/stop/remove).
        win32serviceutil.HandleCommandLine(RemoteViewWindowsService)

