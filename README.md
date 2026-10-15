# RemoteView

Self-hosted screen monitoring: a headless daemon on the home (target) machine
streams compressed screen frames over an encrypted TLS 1.3 tunnel to a viewer
on the remote (operator) machine. It supports screen monitoring plus keyboard
and mouse control.

See `RemoteViewDesign.md` for the full design.

---

## Status

- **Phase 1 (done):** cross-platform core + a working end-to-end stream
  (tested on Linux). Capture → JPEG → TLS → auth → decode all verified.
- **Phase 2 (done, code):** Windows Service layer (un-killable Session-0 "Boss"
  + per-session capture "Helper" with auto-respawn), Linux systemd unit, macOS
  launchd, and `setup.py` host/viewer pairing. Cross-platform parts are tested
  here (the loopback Boss↔Helper transport is tested for real on Linux); the
  **Windows-specific runtime** (pywin32 service, `CreateProcessAsUser` session
  launch) is written to Microsoft's documented pattern and **must be run on a
  Windows host** — it cannot be executed on this Linux box.

## Layout

```
shared/    config.py, crypto.py (TLS + tokens + pairing), protocol.py (frames)
daemon/    capture.py (mss→JPEG), streamer.py (TLS+auth+reconnect), main.py
           transport.py (loopback Boss<->Helper socket), supervisor.py (session launch+respawn)
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

## Complete setup: Windows home machine + Linux viewer

This is the two-machine setup from a blank installation. The Windows home
machine is the screen being monitored. The Linux viewer machine displays it.

### 1. Prepare Windows

Open **Administrator Command Prompt**. Install Git and regular CPython 3.13:

```bat
winget install --id Git.Git --exact
winget install --id Python.Python.3.13 --exact
```

Close and reopen the Administrator Command Prompt, then verify:

```bat
git --version
py -3.13 --version
```

Clone and install RemoteView:

```bat
cd /d "%USERPROFILE%"
git clone https://github.com/Vikaspal8923/RemoteView.git
cd RemoteView
py -3.13 -m pip install -r requirements.txt
```

Use regular Python 3.13 for the Windows service. Do not use Python `3.14t`
(free-threaded Python), because `pywin32` cannot host the service reliably
with that interpreter.

Allow the default network ports if Windows Firewall blocks local-network
connections:

```bat
netsh advfirewall firewall add rule name="RemoteView pairing" dir=in action=allow protocol=TCP localport=9001
netsh advfirewall firewall add rule name="RemoteView viewer" dir=in action=allow protocol=TCP localport=9000
```

### 2. Prepare Linux

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip python3-tk
```

Clone and install RemoteView:

```bash
cd ~/Desktop
git clone https://github.com/Vikaspal8923/RemoteView.git
cd RemoteView
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Close any previous viewer before pairing:

```bash
pkill -f 'viewer.main|setup.py --mode viewer' || true
```

### 3. Generate a pairing code on Windows

RemoteView can detect the host address automatically. The simplest command is:

```bat
cd /d "%USERPROFILE%\RemoteView"
py -3.13 setup.py --mode host
```

Automatic detection first tries the public address and then falls back to a
local network address. On campus Wi-Fi, VPNs, or machines with multiple
adapters, using the Wi-Fi IPv4 address explicitly is safer. Find it with
`ipconfig`, then run:

```bat
cd /d "%USERPROFILE%\RemoteView"
py -3.13 setup.py --mode host --ip 10.125.39.27
```

Replace `10.125.39.27` with the actual Windows Wi-Fi address. Leave this
command running. The printed pairing code can be reused while this window is
open.

### 4. Pair and launch the Linux viewer

Paste the code printed by Windows:

```bash
cd ~/Desktop/RemoteView
source .venv/bin/activate
python setup.py --mode viewer --pair 'RVEW-PASTE-THE-CODE-HERE'
```

The viewer downloads the certificate, key, and config, then opens the viewer
window automatically.

### 5. Install and start the Windows service

After pairing succeeds, use an Administrator Command Prompt on Windows:

```bat
cd /d "%USERPROFILE%\RemoteView"
py -3.13 -m daemon.service.windows install
py -3.13 -m daemon.service.windows start
py -3.13 -m daemon.service.windows status
```

Expected:

```text
RemoteViewDaemon: RUNNING
```

The service launches the interactive capture Helper automatically. Remote
control is enabled by default when the viewer connects. The Linux viewer can
move the mouse, click, scroll, and type into normal (non-Administrator)
Windows applications. Press `F8` in the viewer to toggle remote control;
press `Escape` for the emergency stop. The Windows user's local keyboard and
mouse always have priority: remote input pauses during local activity and
resumes automatically after the user becomes idle. Do not run
`daemon.main --worker` manually during normal use.

Administrator windows are protected by Windows security (UIPI). Test typing
and clicking in a normal Command Prompt or another normal application, not a
Command Prompt launched with **Run as administrator**.

### Remote-control recovery

If the screen still streams but remote clicks or typing stop working, update
and restart both sides. On Windows, use an Administrator Command Prompt:

```bat
cd /d "%USERPROFILE%\RemoteView"
py -3.13 -m daemon.service.windows stop
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'RemoteView|daemon.main' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
git pull
py -3.13 -m pip install -r requirements.txt
py -3.13 -m daemon.service.windows start
py -3.13 -m daemon.service.windows status
```

On Linux:

```bash
cd ~/Desktop/RemoteView
source .venv/bin/activate
git pull
pkill -f 'viewer.main|setup.py --mode viewer' || true
python -m viewer.main --config config.json
```

If Windows input is left stuck after an interrupted older run, reboot Windows
once after stopping the service, then start the service again. Check the logs:

```bat
type "%USERPROFILE%\RemoteView\remoteview-service.log"
type "%USERPROFILE%\RemoteView\remoteview-helper.log"
```

### Ports and recovery

| Port | Machine | Purpose |
|---|---|---|
| 9001 | Windows | Temporary pairing server |
| 9000 | Linux | TLS viewer listener |
| 47921 | Windows only | Local Boss-to-Helper socket |

If Linux port 9000 is busy:

```bash
ss -ltnp | grep ':9000'
pkill -f 'viewer.main|setup.py --mode viewer' || true
```

If Windows port 9001 is busy:

```bat
netstat -ano | findstr :9001
taskkill /F /PID PID_NUMBER
```

Alternatively choose another viewer port:

```bat
py -3.13 setup.py --mode host --ip 10.125.39.27 --viewer-port 9100
```

The pairing server then uses port `9101`, and the viewer uses `9100` from the
downloaded configuration.

For a clean Windows service reinstall:

```bat
py -3.13 -m daemon.service.windows stop
sc delete RemoteViewDaemon
py -3.13 -m daemon.service.windows install
py -3.13 -m daemon.service.windows start
```

For diagnostics:

```bat
type "%USERPROFILE%\RemoteView\remoteview-service.log"
py -3.13 tests\test_windows_smoke.py
```

### Clean previous runs

Use these commands before a fresh test. They stop RemoteView processes and
services without deleting the repository, configuration, or certificates.

#### Windows home machine

Open **Administrator Command Prompt**:

```bat
cd /d "%USERPROFILE%\RemoteView"
py -3.13 -m daemon.service.windows stop
```

Stop remaining RemoteView-related processes:

```bat
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'RemoteView|daemon.main|setup.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
```

Verify:

```bat
tasklist | findstr /I "python pythonservice"
```

#### Linux viewer machine

```bash
pkill -f 'viewer.main|setup.py --mode viewer' || true
pkill -f 'daemon.main|RemoteView' || true
```

Verify no viewer remains and port 9000 is free:

```bash
ps aux | grep -E 'viewer.main|daemon.main|RemoteView' | grep -v grep
ss -ltnp | grep ':9000' || true
```

No output from the final command means the Linux viewer port is available.

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
  `CreateProcessAsUser`; it captures the screen and streams frames to the Boss
  over a **loopback socket** (127.0.0.1, authenticated with the shared token).
  If killed, the Boss respawns it in ~1s.

Install on the home machine (run as Administrator, from the project root):

```bat
python -m daemon.service.windows install
python -m daemon.service.windows start
python -m daemon.service.windows status
```

The service finds `config.json` in this order: the `REMOTEVIEW_CONFIG`
environment variable (if set **machine-wide**), the path recorded at install
time in the service's registry entry, then `<repo>\config.json` (the default
`setup.py` writes). If your config lives outside the repo, set the variable
*before* installing — the value is captured into the registry then, because a
service does **not** inherit the installing shell's environment:

```bat
set REMOTEVIEW_CONFIG=C:\path\to\config.json
python -m daemon.service.windows install
```

Manage: `python -m daemon.service.windows status|stop|restart|remove`, or use
`services.msc`. If the service fails at startup, the reason is logged to Event
Viewer → Windows Logs → Application (source `RemoteViewDaemon`), and `status`
prints the exit codes.

### Before wiring two machines — run the Windows smoke test

On the home Windows machine (Administrator prompt, from the project root):

```bat
pip install -r requirements.txt
python tests\test_windows_smoke.py
```

This validates each Windows layer independently — pywin32/service class, the
loopback frame transport (auth + round-trip), the active-session lookup, and
that `mss` can capture the screen in your session — and tells you exactly which
layer (if any) needs attention before you connect to the viewer machine.

## Security notes

- TLS 1.3 enforced; the daemon pins the viewer's self-signed cert (rejects any
  other). Auth token is constant-time verified before any frame flows.
- **Do not expose `viewer_port` to the public internet.** Use a VPN (Tailscale/
  WireGuard) or firewall-allowlist the home machine's IP.
- The TLS private key is generated on the home (daemon) machine and copied to
  the viewer over the pairing channel (the viewer is the TLS server and must
  present the pinned cert). The current pairing code is reusable while the
  host setup window remains open; close that window when pairing is no longer
  needed.
