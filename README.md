# RemoteView

Self-hosted screen monitoring: a headless daemon on the home (target) machine
streams compressed screen frames over an encrypted TLS 1.3 tunnel to a viewer
on the remote (operator) machine. **Screen monitoring only — no input control.**

See `RemoteViewDesign.md` for the full design.

---

## Status

- **Phase 1 (done):** cross-platform core + a working end-to-end stream
  (tested on Linux). Capture → JPEG → TLS → auth → decode all verified.
- **Phase 2 (done, code):** Windows Service layer (un-killable Session-0 "Boss"
  + per-session capture "Helper" with auto-respawn), Linux systemd unit, macOS
  launchd, and `setup.py` host/viewer pairing. Cross-platform parts are tested
  here; the **Windows-specific runtime** (pywin32 service, `CreateProcessAsUser`
  session launch, named pipe) is written to Microsoft's documented pattern and
  **must be run on a Windows host** — it cannot be executed on this Linux box.

## Layout

```
shared/    config.py, crypto.py (TLS + tokens + pairing), protocol.py (frames)
daemon/    capture.py (mss→JPEG), streamer.py (TLS+auth+reconnect), main.py
           pipe.py (named-pipe transport), supervisor.py (session launch+respawn)
           service/ windows.py (Boss service), linux.py (systemd), macos.py (launchd)
viewer/    receiver.py (TLS listener+auth), display.py (tkinter), main.py
tests/     test_shared.py, test_e2e.py, test_service.py
setup.py   host/viewer CLI + pairing + service install
```

## Setup (Linux dev/test)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

## Run it (Phase 1, single-process foreground mode)

1. Generate a cert + token and write a config:

   ```bash
   python - <<'PY'
   import os, tempfile
   from shared import crypto
   from shared.config import Config
   d = tempfile.mkdtemp()
   crypto.generate_cert_and_key(f"{d}/server.crt", f"{d}/server.key")
   Config(viewer_ip="127.0.0.1", viewer_port=9000,
          auth_token=crypto.generate_auth_token(),
          tls_cert=f"{d}/server.crt", tls_key=f"{d}/server.key"
         ).save(f"{d}/config.json")
   print(f"{d}/config.json")
   PY
   ```

2. **Viewer** (operator machine — shows the window):

   ```bash
   DISPLAY=:0 python -m viewer.main --config <path>/config.json
   ```

3. **Daemon** (home machine — headless):

   ```bash
   DISPLAY=:0 python -m daemon.main --config <path>/config.json --foreground
   ```

   The daemon dials out to `viewer_ip:viewer_port` over TLS 1.3, authenticates
   with the token, and streams frames. The viewer window shows latency + FPS.

## Tests

```bash
python tests/test_shared.py                 # unit: protocol, config, crypto, TLS
python tests/test_service.py                # pairing flow, unit/plist, SDDL, OTP
DISPLAY=:0 python tests/test_e2e.py         # live capture → TLS → decode + auth
```

## Windows deployment (the un-killable Boss + Helper)

The Windows daemon is two processes by design (Session 0 isolation — a service
cannot see the interactive desktop):

- **Boss** — a LocalSystem Windows Service (`RemoteViewDaemon`), Session 0. Holds
  the auth token + TLS, dials out to the viewer, and supervises the Helper. A
  normal-user process **cannot stop it** (only an admin can: services.msc,
  `sc stop RemoteViewDaemon`, Task Manager → Services). It has no window.
- **Helper** — a no-window process launched into your login session via
  `CreateProcessAsUser`; it captures the screen and writes frames to the Boss
  over an ACL-gated named pipe. If killed, the Boss respawns it in ~1s.

Install on the home machine (run as Administrator):

```bat
set REMOTEVIEW_CONFIG=C:\path\to\config.json
python -m daemon.service.windows install
python -m daemon.service.windows start
```

Manage: `python -m daemon.service.windows stop|remove`, or use `services.msc`.

## Security notes

- TLS 1.3 enforced; the daemon pins the viewer's self-signed cert (rejects any
  other). Auth token is constant-time verified before any frame flows.
- **Do not expose `viewer_port` to the public internet.** Use a VPN (Tailscale/
  WireGuard) or firewall-allowlist the home machine's IP.
- The private key stays on the home (daemon) machine only.
