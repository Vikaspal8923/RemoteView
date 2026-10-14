"""RemoteView single-entry-point setup for both machines.

    python setup.py --mode host
    python setup.py --mode viewer --pair RVEW-XXXX-...

Host mode:
  * generates a TLS cert + key + 32-byte auth token (cryptography)
  * auto-detects the host's public IP (best-effort; overridable with --ip)
  * writes config.json
  * installs + starts the OS service (Windows/Linux/macOS)
  * starts a temporary pairing HTTP server on pairing_port (= viewer_port + 1)
    serving the cert + config as a JSON bundle, protected by a one-time token
  * prints the pairing code (RVEW-XXXX-...) and blocks until one viewer pulls

Viewer mode:
  * decodes the pairing code -> ip, port, one-time token, cert fingerprint
  * downloads the JSON bundle from the host's pairing server
  * verifies the downloaded cert against the fingerprint from the code
  * saves cert + config locally
  * launches the viewer
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from shared import crypto
from shared.config import Config



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def detect_public_ip(timeout: float = 5.0) -> str:
    """Best-effort public IP detection via api.ipify.org.

    Falls back to the local LAN IP if the request fails (offline / firewall).
    """
    import urllib.request

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as resp:
            return resp.read().decode("ascii").strip()
    except Exception:
        # Fallback: local IP used to reach a public address (no packets sent).
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# --------------------------------------------------------------------------- #
# Host mode
# --------------------------------------------------------------------------- #
def _make_pairing_handler(state):
    """Build an HTTP handler that serves the bundle to exactly one viewer."""

    class PairingHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default logging
            pass

        def do_GET(self):
            # Require the one-time token as a query param: /bundle?token=<hex>
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            token = (params.get("token") or [""])[0]
            if not crypto.verify_token(
                bytes.fromhex(token) if token else b"", state["otp"].hex()
            ):
                self.send_response(403)
                self.end_headers()
                return
            if state["served"]:
                # One-time: only the first pull succeeds.
                self.send_response(410)
                self.end_headers()
                return
            state["served"] = True
            body = json.dumps(state["bundle"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return PairingHandler


def run_host(args) -> int:
    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    certs_dir = os.path.join(workdir, "certs")
    cert_path = os.path.join(certs_dir, "server.crt")
    key_path = os.path.join(certs_dir, "server.key")
    config_path = os.path.join(workdir, "config.json")

    print("Generating certificate, key, and auth token...")
    crypto.generate_cert_and_key(cert_path, key_path)
    auth_token = crypto.generate_auth_token()

    host_ip = args.ip or detect_public_ip()
    cfg = Config(
        viewer_ip=host_ip,
        viewer_port=args.viewer_port,
        auth_token=auth_token,
        tls_cert=cert_path,
        tls_key=key_path,
        frame_rate=args.frame_rate,
        quality=args.quality,
        monitor=args.monitor,
    )
    cfg.validate()
    cfg.save(config_path)
    print(f"Config saved: {config_path}")

    # One-time token + fingerprint for the pairing code / bundle guard.
    otp = os.urandom(crypto.OTP_SIZE)
    fp = crypto.cert_fingerprint(cert_path)
    pairing_port = args.viewer_port + 1
    code = crypto.encode_pairing_code(host_ip, pairing_port, otp, fp)

    # The bundle the viewer downloads: cert PEM + config JSON (NO private key).
    with open(cert_path, "r", encoding="utf-8") as fh:
        cert_pem = fh.read()
    bundle = {
        "cert_pem": cert_pem,
        "cert_fingerprint": _b64e(fp),
        "config": cfg.to_dict(),
    }
    state = {"otp": otp, "bundle": bundle, "served": False}

    # Start the one-shot pairing server.
    httpd = HTTPServer(("0.0.0.0", pairing_port), _make_pairing_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    print()
    print("✓ Certs generated")
    print("✓ Config saved")
    print("✓ Pairing server listening on port", pairing_port)
    print()
    print("Pairing code (type this on the viewer machine):")
    print()
    print(f"    {code}")
    print()

    # Install + start the OS service (best-effort; may need sudo).
    _install_service(config_path)

    print("Waiting for a viewer to pull the bundle (Ctrl-C to cancel)...")
    try:
        while not state["served"]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        httpd.shutdown()

    if state["served"]:
        print("✓ Viewer paired. The one-time token is now invalid.")
    return 0


def _install_service(config_path: str) -> None:
    """Install + start the OS service for the current platform."""
    if sys.platform.startswith("win"):
        print("Install the Windows service (run as admin):")
        print(f"  set REMOTEVIEW_CONFIG={config_path}")
        print("  python -m daemon.service.windows install")
        print("  python -m daemon.service.windows start")
        print("  python -m daemon.service.windows status")
    elif sys.platform == "darwin":
        print("Installing launchd agent...")
        try:
            from daemon.service import macos
            macos.install(config_path)
        except Exception as exc:
            print(f"  (launchd install skipped: {exc})")
    else:
        print("Install the systemd unit (run as root):")
        print(f"  sudo python -m daemon.service.linux install --config {config_path}")



# --------------------------------------------------------------------------- #
# Viewer mode
# --------------------------------------------------------------------------- #
def run_viewer(args) -> int:
    import http.client

    try:
        decoded = crypto.decode_pairing_code(args.pair)
    except ValueError as exc:
        print(f"error: invalid pairing code: {exc}", file=sys.stderr)
        return 1

    host_ip = decoded["ip"]
    pairing_port = decoded["port"]
    otp_hex = decoded["otp"].hex()
    expected_fp = decoded["fingerprint"]

    workdir = os.path.abspath(args.workdir)
    certs_dir = os.path.join(workdir, "certs")
    os.makedirs(certs_dir, exist_ok=True)
    cert_path = os.path.join(certs_dir, "server.crt")
    config_path = os.path.join(workdir, "config.json")

    print(f"Connecting to pairing server at {host_ip}:{pairing_port} ...")
    try:
        conn = http.client.HTTPConnection(host_ip, pairing_port, timeout=10)
        conn.request("GET", f"/bundle?token={otp_hex}")
        resp = conn.getresponse()
        if resp.status != 200:
            print(f"error: pairing server returned {resp.status}", file=sys.stderr)
            return 1
        bundle = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"error: could not reach pairing server: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Verify the downloaded cert matches the fingerprint baked into the code.
    downloaded_fp = _b64d(bundle["cert_fingerprint"])
    if downloaded_fp != expected_fp:
        print(
            "error: cert fingerprint mismatch -- refusing to trust this host",
            file=sys.stderr,
        )
        return 1

    with open(cert_path, "w", encoding="utf-8") as fh:
        fh.write(bundle["cert_pem"])

    # Save config; point tls_cert at the local copy. The private key is never
    # shipped to the viewer.
    cfg_dict = dict(bundle["config"])
    cfg_dict["tls_cert"] = cert_path
    cfg_dict.pop("tls_key", None)
    cfg = Config(**{k: v for k, v in cfg_dict.items() if k in Config.__dataclass_fields__})
    cfg.config_path = config_path
    cfg.validate()
    cfg.save(config_path)

    print(f"✓ Cert verified + saved: {cert_path}")
    print(f"✓ Config saved: {config_path}")
    print("Launching viewer...")

    # Launch the viewer in-process (receiver thread + tkinter main).
    from viewer.main import main as viewer_main
    return viewer_main(["--config", config_path])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RemoteView setup")
    parser.add_argument("--mode", choices=["host", "viewer"], required=True)
    parser.add_argument("--pair", help="pairing code (viewer mode)")
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument("--ip", help="override host public IP (host mode)")
    parser.add_argument("--viewer-port", type=int, default=9000, dest="viewer_port")
    parser.add_argument("--frame-rate", type=int, default=10, dest="frame_rate")
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--monitor", type=int, default=0)
    args = parser.parse_args(argv)

    if args.mode == "host":
        return run_host(args)
    if args.mode == "viewer":
        if not args.pair:
            print("error: --pair is required in viewer mode", file=sys.stderr)
            return 2
        return run_viewer(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

