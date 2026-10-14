# RemoteView — Screen Monitoring Daemon & Viewer

## Overview

A self-hosted screen monitoring system. Setup uses one host command and one
viewer command connected by a pairing code. On Windows, the daemon is split
into a Session-0 Boss service and an interactive desktop Helper because a
Windows service cannot capture the logged-in desktop directly. Frames are
downscaled, JPEG-compressed, and streamed over TLS to the viewer.

Scope: **screen monitoring only** (v1). No keyboard or mouse control.

---

## Architecture

```
Home Machine (Target)                Remote Machine (Operator)
┌─────────────────────┐              ┌─────────────────────┐
│  OS Service Layer   │              │  Viewer Process     │
│  ┌───────────────┐  │  TLS/TCP     │  ┌───────────────┐  │
│  │    Daemon     │──┼─────────────▶│  │    Viewer     │  │
│  │  (outbound)   │  │  frames      │  │  (listener)   │  │
│  └───────┬───────┘  │              │  └───────┬───────┘  │
│          │          │              │          │          │
│  ┌───────▼───────┐  │              │  ┌───────▼───────┐  │
│  │Screen Capture │  │              │  │Frame Renderer │  │
│  └───────────────┘  │              │  └───────────────┘  │
└─────────────────────┘              └─────────────────────┘
       initiates connection ──────────────────────▶
```

**Key design choice:** the daemon always makes the outbound connection to the
viewer. The viewer listens. The temporary pairing server is the exception and
uses the host's pairing port during setup.

---

## Quick Setup

Two commands — one per machine. The pairing code handles everything in between.

**Home machine (screen being watched)**

```bash
python setup.py --mode host
```

The script can auto-detect an address. For Wi-Fi, VPN, or multi-adapter
machines, an explicit LAN address is safer:

```
✓ Certs generated
✓ Config saved
✓ Daemon installed and started

Pairing code:  RVEW-A3B2-C1D4-E5F6-G7H8-I9J0

Type this on your viewer machine.
```

**Remote machine (watching the screen)**

```bash
python setup.py --mode viewer --pair RVEW-A3B2-C1D4-E5F6-G7H8-I9J0
```

Decodes the code, pulls cert and config from the home machine automatically, saves them, and opens the viewer window. No file copying.

**Stopping the daemon**

| OS | Command |
| --- | --- |
| Windows | `net stop RemoteViewDaemon` |
| Linux | `systemctl stop remoteview` |
| macOS | `launchctl stop com.remoteview.daemon` |

---

## ## Connection Flow — Technical Detail

**Phase 1 — Pairing**

1. `setup.py --mode host` generates TLS cert + key + auth token, selects the host IP, and builds a pairing payload displayed as `RVEW-...`
2. A temporary HTTP server starts on `pairing_port` (`viewer_port + 1`) and serves the cert, private key, and config bundle protected by the pairing token
3. `setup.py --mode viewer --pair <code>` decodes the code and connects to the pairing server
4. The viewer downloads and verifies the bundle
5. Viewer verifies the cert against the fingerprint from the code; saves cert + key + config locally (the viewer is the TLS server, so it needs the key to present the pinned cert)
6. The pairing code can be reused while the host setup window remains open; closing that process stops the pairing server

**Phase 2 — Streaming (every session)**

1. Viewer starts its TLS listener on `viewer_port`
2. Daemon (running as OS service) makes outbound TCP connection to `viewer_ip:viewer_port`
3. TLS 1.3 handshake completes; daemon pins the cert presented by the viewer (rejects any other)
4. Daemon sends 32-byte HMAC auth token; viewer verifies with constant-time compare
5. Viewer sends `AUTH_OK`; frame stream begins
6. On disconnect: daemon auto-reconnects with exponential backoff (1s → 2s → 4s … max 30s)

---

## Project File Structure

```
remoteview/
├── setup.py                   # Single entry point for both machines
├── requirements.txt
│
├── shared/
│   ├── config.py              # Config dataclass + loader
│   ├── crypto.py              # TLS helpers, token verify, pairing encode/decode
│   └── protocol.py            # Frame packet: pack / unpack
│
├── daemon/                    # Runs on HOME machine
│   ├── main.py                # Starts capture + streamer tasks
│   ├── capture.py             # mss screen capture loop → frame queue
│   ├── streamer.py            # Reads queue, sends frames over TLS socket
│   └── service/
│       ├── windows.py         # Windows Service wrapper (pywin32)
│       ├── linux.py           # Writes + enables systemd unit file
│       └── macos.py           # Writes + loads launchd plist
│
└── viewer/                    # Runs on REMOTE machine
    ├── main.py                # Starts receiver + display
    ├── receiver.py            # TLS listener, auth, frame queue filler
    └── display.py             # Frame renderer (tkinter)
```

---

## Wire Protocol

### Auth Handshake (after TLS)

```
Viewer  ──[TLS established]──▶  Daemon
Daemon  ──[ 32-byte HMAC token ]──▶  Viewer
Viewer  ──[ b"AUTH_OK" or b"AUTH_FAIL" ]──▶  Daemon
```

If `AUTH_FAIL`: daemon closes socket and retries after backoff.

### Frame Packet (binary, big-endian)

```
┌──────────────┬───────────────────────┬──────────────┐
│  4 bytes     │  N bytes              │  8 bytes     │
│  frame_size  │  JPEG/WebP payload    │  unix_ms     │
└──────────────┴───────────────────────┴──────────────┘
```

- `frame_size`: uint32, length of the compressed image payload
- payload: Pillow-compressed JPEG (configurable quality) or WebP
- `unix_ms`: uint64, capture timestamp (milliseconds) for latency tracking

Receiver reads `frame_size` first, then reads exactly that many bytes, then reads 8-byte timestamp.

---

## Configuration (`config.json`)

Both machines use the same file. Generate once, copy to both.

```json
{
  "viewer_ip":   "203.0.113.10",
  "viewer_port": 9000,
  "auth_token":  "<64-char hex string — output of generate_certs.py>",
  "tls_cert":    "certs/server.crt",
  "tls_key":     "certs/server.key",
  "frame_rate":  5,
  "quality":     45,
  "max_width":   1280,
  "max_height":  720,
  "monitor":     0
}
```

| Field | Used by | Notes |
| --- | --- | --- |
| `viewer_ip` | Daemon | IP the daemon dials out to |
| `viewer_port` | Both | Viewer listens here; daemon connects here |
| `auth_token` | Both | 32-byte token; must match on both sides |
| `tls_cert` | Both | Viewer presents it as TLS server; daemon pins it |
| `tls_key` | Daemon + viewer | Generated on home; shipped to viewer during pairing |
| `frame_rate` | Daemon | Capture FPS; default 5 for lower latency |
| `quality` | Daemon | JPEG quality; default 45 for lower latency |
| `max_width`, `max_height` | Daemon | Stream downscale limit; defaults to 1280×720 |
| `monitor` | Daemon | 0 = primary monitor |

---

## Module Responsibilities

### `setup.py`

- CLI: `python setup.py --mode host` or `python setup.py --mode viewer --pair <code>`
- **Host mode**: generates TLS cert + key + auth token; `--ip` embeds this machine's address in the pairing code (default: automatic public/local detection); writes `config.json`; records the viewer address when it pulls the bundle; and runs the pairing server while the host command remains open
- **Viewer mode**: decodes the pairing code (base32 → IP, port, one-time token, cert fingerprint); connects to the pairing HTTP server; downloads and verifies the cert + key + config bundle; saves to disk; launches `viewer/main.py`
- Pairing code format: `base32(ip_bytes + port_uint16 + token_16bytes + fingerprint_4bytes)` → grouped in 4-char blocks, e.g. `RVEW-A3B2-C1D4-E5F6-G7H8-I9J0`
- Pairing token: reusable while the host pairing server remains running

### `shared/config.py`

- `@dataclass Config` with all fields
- `Config.load(path: str) -> Config` reads and validates JSON
- Raises `ValueError` on missing required fields

### `shared/crypto.py`

- `verify_token(received: bytes, expected_hex: str) -> bool` — constant-time compare
- `build_ssl_context_server(cert, key) -> ssl.SSLContext` — TLS 1.3, server side
- `build_ssl_context_client(cert) -> ssl.SSLContext` — TLS 1.3, client side, pins cert

### `shared/protocol.py`

- `pack_frame(image_bytes: bytes, timestamp_ms: int) -> bytes`
- `unpack_frame_header(data: bytes) -> tuple[int, int]` — returns (frame\_size, timestamp\_ms)
- Constants: `HEADER_SIZE = 12` (4 + 8)

### `daemon/capture.py`

- Uses `mss` to grab the configured monitor
- Compresses with `Pillow` to JPEG bytes at configured quality
- Pushes `(frame_bytes, timestamp_ms)` into an `asyncio.Queue`
- Respects `frame_rate`: sleeps `1/frame_rate` seconds between captures
- Runs as an `asyncio` task

### `daemon/streamer.py`

- Opens TLS TCP connection to `viewer_ip:viewer_port`
- Performs auth handshake (sends token, waits for `AUTH_OK`)
- Reads from the capture queue; sends packed frames
- On disconnect: exponential backoff (1s, 2s, 4s … max 30s), then reconnects
- Runs as an `asyncio` task

### `daemon/main.py`

- CLI: `python main.py --config config.json`
- Loads config, creates queue, starts `capture` and `streamer` tasks
- `asyncio.run(main())` — no GUI, no window, clean process
- On Linux/macOS, daemonizes by double-forking when invoked by service manager

### `viewer/receiver.py`

- Opens TLS listener on `0.0.0.0:viewer_port`
- Accepts one connection at a time
- Verifies auth token; sends `AUTH_OK` or `AUTH_FAIL`
- Reads frame packets in a loop; pushes decoded image bytes into an `asyncio.Queue`
- Runs as an `asyncio` task

### `viewer/display.py`

- Reads from the receiver queue
- Decodes JPEG bytes to a `PIL.Image`, converts to `ImageTk.PhotoImage`
- Renders in a `tkinter` canvas — resizes canvas to match the received frame dimensions
- Title bar shows latency (`unix_ms` delta) and FPS
- `Q` key or window close → triggers clean shutdown
- Runs on the main thread (tkinter requirement)

### `viewer/main.py`

- CLI: `python main.py --config config.json`
- Starts `receiver` as a background thread (wrapping asyncio loop)
- Runs `display` on main thread

---

## OS Service Installation

The host setup generates the config and prints the service commands. On
Windows, install/start those commands from an Administrator Command Prompt.
The commands below are for managing the service after installation.

### Windows — `daemon/service/windows.py`

The Boss runs as the registered `RemoteViewDaemon` Windows Service under
LocalSystem. It supervises a hidden Helper launched into the interactive user
session. The service is not a second screen window and it is not immune to an
administrator stopping the service.

```bash
# Stop
net stop RemoteViewDaemon

# Restart
net start RemoteViewDaemon
```

### Linux — `daemon/service/linux.py`

Writes and enables a systemd unit file at `/etc/systemd/system/remoteview.service`.

```bash
systemctl stop remoteview
systemctl start remoteview   # restart if needed
```

### macOS — `daemon/service/macos.py`

Writes and loads a launchd plist at `~/Library/LaunchAgents/com.remoteview.daemon.plist`.

```bash
launchctl stop com.remoteview.daemon
launchctl start com.remoteview.daemon   # restart if needed
```

---

## Requirements

```
mss>=9.0
Pillow>=10.0
cryptography>=42.0
pywin32>=306       # Windows only
```

---

## Security Constraints

- TLS 1.3 enforced; no fallback to lower versions
- Viewer pins the self-signed cert — rejects any other cert on reconnect
- Auth token verified before any frame data flows
- Do not expose `viewer_port` to the public internet. Options:
  - Allowlist only the home machine's IP in the remote machine's firewall
  - Or run both over a VPN (Tailscale/WireGuard) and bind to the VPN interface only
- The viewer receives the TLS private key during pairing because the viewer is
  the TLS server and must present the pinned certificate. Protect the viewer
  config directory accordingly.

---

## Out of Scope — v1

- Keyboard / mouse control (v2)
- Multi-monitor switching UI (v2)
- Audio streaming (future)
- Mobile viewer (future)
- Web-based viewer (future)

## Next Goal — Limited Keyboard and Mouse Control

The next feature will add optional remote control while keeping RemoteView
strictly limited to screen monitoring plus keyboard and mouse input. Audio,
camera, clipboard, file transfer, shell commands, and process control remain
out of scope.

### Control architecture

```text
Linux Viewer
  ├─ displays screen frames
  ├─ captures local mouse/keyboard events
  └─ sends authenticated control commands over TLS
             ↓
Windows Boss service
  ├─ authenticates and validates commands
  └─ forwards commands to the Helper
             ↓
Windows Helper in the logged-in user session
  └─ performs input through the Windows SendInput API
```

The Boss must not inject input directly. It runs in Session 0, while the
Helper runs in the user’s interactive desktop session. The Helper is therefore
the only component allowed to call `SendInput`.

### Allowed command types

Only these control messages are planned:

```text
MOUSE_MOVE
MOUSE_BUTTON_DOWN
MOUSE_BUTTON_UP
MOUSE_SCROLL
KEY_DOWN
KEY_UP
TYPE_TEXT
```

There will be no messages for shell execution, process termination, window
management, clipboard access, file transfer, audio, or camera control.

### Coordinate and keyboard model

The viewer displays a scaled stream, so mouse positions will be sent as
normalized coordinates:

```text
x: 0.0 .. 1.0
y: 0.0 .. 1.0
```

The Windows Helper converts those values to the selected monitor’s actual
pixel coordinates. Keyboard commands will use stable key names or validated
Windows virtual-key identifiers, with separate key-down and key-up events.

### Control state and safety

Control is disabled by default. The viewer must show a clear state such as
`MONITORING ONLY` or `REMOTE CONTROL ACTIVE`. The design requires:

- explicit user confirmation before enabling control;
- a visible enable/disable control on the viewer;
- an emergency stop action;
- automatic disable when the TLS connection drops;
- release of all held keys and mouse buttons on disconnect;
- validation of coordinates, key values, and event rates;
- no control commands accepted before control is explicitly enabled;
- logging of enable, disable, disconnect, and emergency-stop events.

### Protocol direction

The existing authenticated TLS connection will become bidirectional:

```text
Windows → Linux: frame packets
Linux → Windows: validated control packets
```

Implementation will be staged as follows:

1. Add control packet types and bidirectional transport tests.
2. Add Windows Helper input injection behind a disabled-by-default flag.
3. Add viewer mouse/keyboard event capture and coordinate mapping.
4. Add explicit enable/disable UI and emergency stop behavior.
5. Test disconnects, held keys, multiple monitors, locked screens, and
   malformed or high-rate commands.
