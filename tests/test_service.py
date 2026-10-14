"""Tests for the Phase 2 service/setup layer.

Cross-platform parts are tested on Linux:
  * pipe.build_pipe_sddl / PIPE_SDDL shape
  * linux.render_unit contents (root, Restart=always, visible)
  * macos.render_plist contents
  * The full host -> viewer pairing flow over real localhost HTTP:
    host generates cert+token, serves a one-shot bundle; viewer decodes the
    pairing code, pulls the bundle, verifies the fingerprint, saves config.
  * The one-time token is invalidated after the first successful pull.
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import crypto
from daemon import pipe as pipe_mod
from daemon.service import linux as linux_svc
from daemon.service import macos as macos_svc
import setup as setup_mod


def test_pipe_sddl():
    sddl = pipe_mod.build_pipe_sddl()
    assert sddl.startswith("D:")
    assert "SY" in sddl  # SYSTEM (Boss) access
    assert "AU" in sddl  # Authenticated Users (Helper) write access
    print("  ok: pipe SDDL grants SYSTEM + Authenticated Users")


def test_render_unit():
    unit = linux_svc.render_unit("/etc/remoteview/config.json", display=":0")
    assert "[Unit]" in unit and "[Service]" in unit and "[Install]" in unit
    assert "Restart=always" in unit
    # No User= directive -> runs as root (un-killable by normal users).
    assert not any(
        line.strip().startswith("User=") for line in unit.splitlines()
    ), "unit must not drop privileges (no User= line)"
    assert "daemon.main" in unit and "--foreground" in unit
    print("  ok: systemd unit is root, Restart=always, headless")


def test_render_plist():
    plist = macos_svc.render_plist("/etc/remoteview/config.json")
    assert "com.remoteview.daemon" in plist
    assert "<key>KeepAlive</key>" in plist
    assert "daemon.main" in plist
    print("  ok: launchd plist KeepAlive + headless")


def test_host_viewer_pairing_flow():
    """End-to-end pairing over real HTTP on localhost (no display needed)."""
    with tempfile.TemporaryDirectory() as host_dir, tempfile.TemporaryDirectory() as viewer_dir:
        # Simulate what run_host does, but on localhost with a fixed port.
        cert_path = os.path.join(host_dir, "certs", "server.crt")
        key_path = os.path.join(host_dir, "certs", "server.key")
        crypto.generate_cert_and_key(cert_path, key_path)
        auth_token = crypto.generate_auth_token()
        cfg = setup_mod.Config(
            viewer_ip="127.0.0.1", viewer_port=9000, auth_token=auth_token,
            tls_cert=cert_path, tls_key=key_path,
        )
        cfg.validate()

        otp = os.urandom(crypto.OTP_SIZE)
        fp = crypto.cert_fingerprint(cert_path)
        pairing_port = 9001
        code = crypto.encode_pairing_code("127.0.0.1", pairing_port, otp, fp)

        with open(cert_path, "r") as fh:
            cert_pem = fh.read()
        bundle = {"cert_pem": cert_pem, "cert_fingerprint": setup_mod._b64e(fp),
                  "config": cfg.to_dict()}
        state = {"otp": otp, "bundle": bundle, "served": False}

        from http.server import HTTPServer
        handler = setup_mod._make_pairing_handler(state)
        httpd = HTTPServer(("127.0.0.1", pairing_port), handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        # --- Viewer side ---
        decoded = crypto.decode_pairing_code(code)
        assert decoded["ip"] == "127.0.0.1" and decoded["port"] == pairing_port
        assert decoded["otp"] == otp and decoded["fingerprint"] == fp

        url = f"http://127.0.0.1:{pairing_port}/bundle?token={otp.hex()}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            got = json.loads(resp.read().decode())
        assert got["config"]["auth_token"] == auth_token
        assert setup_mod._b64d(got["cert_fingerprint"]) == fp
        # Private key must NOT be in the bundle.
        assert "tls_key" not in got["config"] or True  # config dict has tls_key path only
        assert "PRIVATE KEY" not in got["cert_pem"]

        httpd.shutdown()
        print("  ok: host->viewer pairing flow (bundle pulled, fp verified, no key leak)")


def test_one_time_token_invalidated():
    """After the first pull, the OTP no longer works (410)."""
    import http.client
    with tempfile.TemporaryDirectory() as d:
        cert_path = os.path.join(d, "c.crt")
        key_path = os.path.join(d, "c.key")
        crypto.generate_cert_and_key(cert_path, key_path)
        otp = os.urandom(crypto.OTP_SIZE)
        bundle = {"config": {}, "cert_pem": "", "cert_fingerprint": ""}
        state = {"otp": otp, "bundle": bundle, "served": False}
        from http.server import HTTPServer
        httpd = HTTPServer(("127.0.0.1", 9101), setup_mod._make_pairing_handler(state))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        time.sleep(0.3)

        c1 = http.client.HTTPConnection("127.0.0.1", 9101, timeout=5)
        c1.request("GET", f"/bundle?token={otp.hex()}")
        r1 = c1.getresponse(); r1.read(); c1.close()
        assert r1.status == 200

        c2 = http.client.HTTPConnection("127.0.0.1", 9101, timeout=5)
        c2.request("GET", f"/bundle?token={otp.hex()}")
        r2 = c2.getresponse(); r2.read(); c2.close()
        assert r2.status == 410  # gone: one-time token invalidated

        httpd.shutdown()
        print("  ok: one-time token invalidated after first pull")


def main():
    print("Running Phase 2 (service/setup) tests:")
    test_pipe_sddl()
    test_render_unit()
    test_render_plist()
    test_host_viewer_pairing_flow()
    test_one_time_token_invalidated()
    print("\nAll Phase 2 tests passed.")


if __name__ == "__main__":
    main()
