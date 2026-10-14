"""Windows smoke tests -- RUN THIS ON THE HOME WINDOWS MACHINE.

The Windows-specific runtime (pywin32 service, CreateProcessAsUser session
launch, loopback-socket transport) cannot be exercised on Linux, so this script
lets you validate each layer independently on the actual target before wiring
up two machines. Run in an Administrator prompt from the project root:

    python tests/test_windows_smoke.py

It checks, in order:
    1. pywin32 is importable and the service class builds.
    2. The loopback frame transport round-trips (server <-> client) in one
       process (validates transport.py + framing without needing the service).
    3. The Boss can find the active console session (WTSGetActiveConsoleSessionId).
    4. Capture works in THIS session (mss) -- proves screen capture is possible.

Each check prints PASS/FAIL and the script exits non-zero on any failure.
"""

import os
import struct
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []


def check(name):
    def deco(fn):
        def wrapper():
            try:
                fn()
                RESULTS.append((name, True, ""))
                print(f"  PASS: {name}")
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, False, repr(exc)))
                print(f"  FAIL: {name} -> {exc!r}")
        wrapper._is_check = True
        return wrapper
    return deco


@check("pywin32 importable + service class builds")
def _test_service_class():
    if not sys.platform.startswith("win"):
        raise RuntimeError("not on Windows")
    from daemon.service import windows
    cls = windows.build_service_class()
    assert cls.__name__ == "RemoteViewWindowsService"
    # ServiceFramework exposes HandleCommandLine on the class.
    assert hasattr(cls, "SvcDoRun") and hasattr(cls, "SvcStop")


@check("loopback frame transport round-trip (transport.py)")
def _test_socket_roundtrip():
    from daemon import transport as transport_mod

    server_ready = threading.Event()
    received = {}
    token = "ab" * 32

    def server():
        listen_sock = transport_mod.create_server()
        server_ready.set()
        conn = transport_mod.accept_client(listen_sock)
        if conn is not None and transport_mod.server_authenticate(conn, token):
            received["frame"] = transport_mod.read_frame_blocking(conn)
        transport_mod.close_handle(conn)
        transport_mod.close_handle(listen_sock)

    t = threading.Thread(target=server, daemon=True)
    t.start()
    server_ready.wait(timeout=5)
    time.sleep(0.2)  # let the listener bind

    sock = transport_mod.connect_client()
    assert transport_mod.client_authenticate(sock, token), "client auth failed"
    writer = transport_mod.FrameSocketWriter(sock)
    payload = b"JPEGTESTDATA" * 100
    ts = 123456789
    writer.put(payload, ts)
    writer.close()

    t.join(timeout=5)
    assert received.get("frame") is not None, "no frame received on the socket"
    got_payload, got_ts = received["frame"]
    assert got_payload == payload
    assert got_ts == ts


@check("active console session id resolvable (WTS)")
def _test_session():
    from daemon import supervisor
    sid = supervisor.get_active_session_id()
    assert isinstance(sid, int) and sid != 0xFFFFFFFF, f"bad session id {sid}"


@check("screen capture works in this session (mss)")
def _test_capture():
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    assert img.width > 0 and img.height > 0


def main():
    print("RemoteView Windows smoke tests (run as Administrator):\n")
    if not sys.platform.startswith("win"):
        print("ERROR: this script must run on Windows.", file=sys.stderr)
        return 2
    for name, fn in list(globals().items()):
        if callable(fn) and getattr(fn, "_is_check", False):
            fn()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} checks passed.")
    if passed != total:
        print("\nFailed checks:")
        for name, ok, err in RESULTS:
            if not ok:
                print(f"  - {name}: {err}")
        return 1
    print("\nAll Windows layers OK. Safe to proceed to a two-machine test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
