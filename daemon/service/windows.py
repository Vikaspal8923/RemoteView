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

Manage: python -m daemon.service.windows install|update|start|stop|restart|status|remove|debug
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import queue
from typing import Optional

SERVICE_NAME = "RemoteViewDaemon"
DISPLAY_NAME = "RemoteView Daemon"
SERVICE_DESCRIPTION = "RemoteView screen monitoring daemon (un-killable Boss)."

# --- Bootstrap for the pythonservice.exe host --------------------------------
# When installed via `python -m daemon.service.windows install`, pywin32 records
# this file's *path* (not a dotted module) in the service's PythonClass registry
# value, because the module was running as __main__. When the SCM starts the
# service, pythonservice.exe therefore adds only <root>\daemon\service to
# sys.path and imports this file as top-level `windows` -- so the project root
# (needed by the lazy `shared.*` / `daemon.*` imports in run()) would be missing
# from sys.path, and the process CWD is C:\Windows\system32. Put the project
# root on sys.path at import time so the service can import its own packages no
# matter how it was launched. Harmless elsewhere (the path is already present).
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _choose_config_path(env_value, registry_value, project_root):
    """Pick the config path for the service process.

    Precedence:
      1. ``REMOTEVIEW_CONFIG`` environment variable (explicit override),
      2. the value persisted at install time (pywin32 SetServiceCustomOption),
         because a service does NOT inherit the shell that ran ``install``,
      3. ``<project root>/config.json`` -- the default setup.py writes.
    """
    if env_value:
        return os.path.abspath(env_value)
    if registry_value:
        return registry_value
    return os.path.join(project_root, "config.json")


def _resolve_config_path():
    """Resolve config.json for this process (env > install-time registry > repo default)."""
    registry_value = None
    if sys.platform.startswith("win"):
        try:
            import win32serviceutil  # type: ignore

            registry_value = win32serviceutil.GetServiceCustomOption(
                SERVICE_NAME, "config"
            )
        except Exception:
            registry_value = None
    return _choose_config_path(
        os.environ.get("REMOTEVIEW_CONFIG"), registry_value, _PROJECT_ROOT
    )


def _persist_config_path(win32serviceutil) -> None:
    """Record the config path chosen at install/update time in the registry.

    Services are started by the SCM under the System account: they do not see
    the ``set REMOTEVIEW_CONFIG=...`` of the admin shell that installed them,
    and their CWD is system32. pywin32's custom-option key
    (Services\\<name>\\Parameters) survives restarts and is read back by
    _resolve_config_path() at service runtime.
    """
    value = _choose_config_path(
        os.environ.get("REMOTEVIEW_CONFIG"), None, _PROJECT_ROOT
    )
    win32serviceutil.SetServiceCustomOption(SERVICE_NAME, "config", value)
    print(f"Recorded service config path: {value}")


def _apply_config_dir(cfg, config_path: str):
    """Resolve relative tls_cert/tls_key against the config file's directory.

    The service runs with CWD=C:\\Windows\\system32, so a relative path inside
    config.json would never be found there.
    """
    base = os.path.dirname(os.path.abspath(config_path))
    if cfg.tls_cert and not os.path.isabs(cfg.tls_cert):
        cfg.tls_cert = os.path.join(base, cfg.tls_cert)
    if cfg.tls_key and not os.path.isabs(cfg.tls_key):
        cfg.tls_key = os.path.join(base, cfg.tls_key)
    return cfg


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

        cfg = _apply_config_dir(Config.load(self._config_path), self._config_path)
        self._log(f"starting with config={self._config_path}, viewer={cfg.viewer_ip}:{cfg.viewer_port}")

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

    def _log(self, message: str) -> None:
        """Write diagnostics somewhere useful when the service has no console."""
        line = f"[{__import__('datetime').datetime.now().isoformat(timespec='seconds')}] {message}\n"
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(self._config_path)), "remoteview-service.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
        try:
            print(f"[boss] {message}", flush=True)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    async def _run_until_stop(self, loop, frame_queue, source, cfg):
        from daemon import streamer as streamer_mod

        control_queue = queue.Queue()
        pump_task = asyncio.ensure_future(self._frame_pump(loop, frame_queue, cfg, control_queue))
        stream_stop = asyncio.Event()

        def start_stream(current_cfg):
            def handle_control(msg):
                self._log(f"control received: {msg.type}")
                control_queue.put(msg)
            return asyncio.ensure_future(
                streamer_mod.stream_frames(
                    current_cfg.viewer_ip,
                    current_cfg.viewer_port,
                    current_cfg.auth_token,
                    current_cfg.tls_cert,
                    source,
                    stream_stop,
                    control_handler=handle_control,
                )
            )

        stream_task = start_stream(cfg)
        try:
            config_mtime = os.path.getmtime(self._config_path)
        except OSError:
            config_mtime = None
        # Poll the stop flag; also exit if the stream task ends unexpectedly.
        while not self._stop.is_set() and not stream_task.done():
            await asyncio.sleep(0.25)
            try:
                new_mtime = os.path.getmtime(self._config_path)
            except OSError:
                new_mtime = config_mtime
            if config_mtime is not None and new_mtime != config_mtime:
                # setup.py rewrites viewer_ip after pairing. Reload it in the
                # already-running service so a manual restart is unnecessary.
                config_mtime = new_mtime
                from shared.config import Config
                new_cfg = _apply_config_dir(Config.load(self._config_path), self._config_path)
                self._log(f"config changed; reconnecting viewer={new_cfg.viewer_ip}:{new_cfg.viewer_port}")
                stream_stop.set()
                stream_task.cancel()
                try:
                    await stream_task
                except asyncio.CancelledError:
                    pass
                stream_stop = asyncio.Event()
                stream_task = start_stream(new_cfg)
        if stream_task.done() and not self._stop.is_set():
            # Do not silently turn a crashed streamer into a clean service exit.
            # The service wrapper records this exception in the Event Log and
            # status then reports the real failure instead of an unexplained 1.
            stream_task.result()
        pump_task.cancel()
        stream_task.cancel()
        for t in (pump_task, stream_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def _frame_pump(self, loop, frame_queue: asyncio.Queue, cfg, control_queue):
        """Create + accept the loopback socket; read frames into frame_queue.

        Blocking socket I/O is offloaded to a thread executor so it does not
        stall the asyncio loop driving the TLS streamer. The Helper must present
        the shared auth token before any frames are accepted.
        """
        from daemon import transport as transport_mod

        while not self._stop.is_set():
            try:
                listen_sock = await loop.run_in_executor(None, transport_mod.create_server)
            except OSError as exc:
                self._log(f"frame channel bind failed: {exc!r}")
                await asyncio.sleep(1.0)
                continue
            conn = await loop.run_in_executor(None, transport_mod.accept_client, listen_sock)
            if conn is None:
                transport_mod.close_handle(listen_sock)
                await asyncio.sleep(0.25)
                continue
            # Authenticate the Helper before reading any frames.
            try:
                authed = await loop.run_in_executor(
                    None, transport_mod.server_authenticate, conn, cfg.auth_token
                )
            except Exception as exc:
                self._log(f"helper authentication error: {exc!r}")
                authed = False
            if not authed:
                print("[boss] Helper failed auth; dropping", flush=True)
                transport_mod.close_handle(conn)
                transport_mod.close_handle(listen_sock)
                await asyncio.sleep(0.25)
                continue
            sender_stop = threading.Event()
            def send_controls():
                while not sender_stop.is_set():
                    try:
                        msg = control_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    try:
                        conn.sendall(msg.encode())
                    except OSError:
                        return
            sender = threading.Thread(target=send_controls, daemon=True, name="helper-control-sender")
            sender.start()
            print("[boss] Helper connected + authenticated; reading frames", flush=True)
            while not self._stop.is_set():
                try:
                    item = await loop.run_in_executor(
                        None, transport_mod.read_frame_blocking, conn
                    )
                except Exception as exc:
                    self._log(f"helper frame read error: {exc!r}")
                    break
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
            sender_stop.set()
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
            # NOTE: the service does not inherit the installing shell's env
            # (the SCM starts it with System-account env and CWD=system32), so
            # this checks the install-time registry value and falls back to
            # <project root>\config.json. See _resolve_config_path.
            config_path = _resolve_config_path()
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
            # Guarded: a service has no console, so an exception (or a run()
            # that ends without a stop request) would otherwise be invisible --
            # the service would sit at RUNNING while doing nothing. Log to the
            # Event Log and signal the stop event so `status` shows the truth.
            def _run_guarded():
                try:
                    self.run()
                    unexpected = not self._stop.is_set()
                    reason = "run() ended without a stop request"
                except Exception as exc:  # noqa: BLE001
                    unexpected = True
                    reason = f"crashed: {exc!r}"
                    self._log(reason)
                if unexpected:
                    try:
                        servicemanager.LogErrorMsg(f"{self._svc_name_}: {reason}")
                    except Exception:
                        pass
                    win32event.SetEvent(self.hWaitStop)

            work = threading.Thread(target=_run_guarded, name="boss-run", daemon=True)
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


# When pythonservice.exe loads this file from the SCM, it imports it as the
# top-level module ``windows``; ``__name__ == '__main__'`` is false in that
# process.  Export the class during normal Windows module import as well as
# during CLI management commands, otherwise pywin32 cannot resolve the
# PythonClass registry value and the service exits immediately.
if sys.platform.startswith("win"):
    RemoteViewWindowsService = build_service_class()


if __name__ == "__main__":
    if not sys.platform.startswith("win"):
        print("daemon.service.windows is Windows-only.", file=sys.stderr)
        raise SystemExit(2)

    _sm, _we, _ws, win32serviceutil = _load_win32service()
    # HandleCommandLine/debug resolves the service class by looking it up on
    # the imported module.  A local variable is not enough: without exporting
    # this name, pywin32 reports "module 'windows' has no attribute
    # 'RemoteViewWindowsService'" and the service exits before SvcDoRun.
    if "RemoteViewWindowsService" not in globals():
        RemoteViewWindowsService = build_service_class()
    globals()["RemoteViewWindowsService"] = RemoteViewWindowsService

    if len(sys.argv) == 1:
        # Started by the SCM: hand control to pywin32's service dispatcher.
        _sm.Initialize()
        _sm.PrepareToHostSingleService(RemoteViewWindowsService)
        _sm.StartServiceCtrlDispatcher()
    else:
        # Management command from an admin shell (install/start/stop/...).
        cmd = sys.argv[1]

        if cmd == "status":
            # pywin32's HandleCommandLine has no 'status' verb; query the SCM.
            try:
                status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
            except Exception as exc:
                print(f"{SERVICE_NAME} is not installed: {exc}", file=sys.stderr)
                raise SystemExit(1)
            states = {
                _ws.SERVICE_STOPPED: "STOPPED",
                _ws.SERVICE_START_PENDING: "START_PENDING",
                _ws.SERVICE_STOP_PENDING: "STOP_PENDING",
                _ws.SERVICE_RUNNING: "RUNNING",
                _ws.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",
                _ws.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
                _ws.SERVICE_PAUSED: "PAUSED",
            }
            name = states.get(status[1], str(status[1]))
            print(f"{SERVICE_NAME}: {name}")
            if status[1] == _ws.SERVICE_STOPPED and (status[3] or status[4]):
                print(f"  exit codes: win32={status[3]} service={status[4]}")
            raise SystemExit(0)

        err = win32serviceutil.HandleCommandLine(RemoteViewWindowsService)
        if not err and {"install", "update"} & set(sys.argv[1:]):
            # Remember where config.json lives: the service cannot inherit this
            # shell's REMOTEVIEW_CONFIG, so persist the resolved absolute path
            # to the service's Parameters registry key.
            try:
                _persist_config_path(win32serviceutil)
            except Exception as exc:
                print(f"Warning: could not record config path: {exc}", file=sys.stderr)
        raise SystemExit(err or 0)

