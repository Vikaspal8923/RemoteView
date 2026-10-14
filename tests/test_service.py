"""Tests for the Phase 2 service/setup layer.

Cross-platform parts are tested on Linux:
  * transport: loopback socket round-trip + auth (the Boss<->Helper channel)
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
from daemon import transport as transport_mod
from daemon.service import linux as linux_svc
from daemon.service import macos as macos_svc
import setup as setup_mod


def test_loopback_transport_roundtrip():
    """Boss(server) <-> Helper(client) over a loopback socket, with auth."""
    server_ready = threading.Event()
    received = {}
    token = crypto.generate_auth_token()

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
    time.sleep(0.2)

    sock = transport_mod.connect_client()
    assert transport_mod.client_authenticate(sock, token), "client auth failed"
    writer = transport_mod.FrameSocketWriter(sock)
    payload = os.urandom(4096)
    ts = 987654321
    writer.put(payload, ts)
    writer.close()

    t.join(timeout=5)
    assert received.get("frame") is not None, "no frame received on the socket"
    got_payload, got_ts = received["frame"]
    assert got_payload == payload
    assert got_ts == ts
    print("  ok: loopback transport round-trip (auth + framed frames)")


def test_loopback_transport_rejects_bad_token():
    """A client with the wrong token must be rejected and get no frames."""
    server_ready = threading.Event()
    received = {}
    good_token = crypto.generate_auth_token()
    bad_token = crypto.generate_auth_token()

    def server():
        listen_sock = transport_mod.create_server()
        server_ready.set()
        conn = transport_mod.accept_client(listen_sock)
        # Auth should fail; do not read frames.
        if conn is not None and transport_mod.server_authenticate(conn, good_token):
            received["frame"] = transport_mod.read_frame_blocking(conn)
        transport_mod.close_handle(conn)
        transport_mod.close_handle(listen_sock)

    t = threading.Thread(target=server, daemon=True)
    t.start()
    server_ready.wait(timeout=5)
    time.sleep(0.2)

    sock = transport_mod.connect_client()
    ok = transport_mod.client_authenticate(sock, bad_token)
    assert ok is False, "bad token must be rejected"
    transport_mod.close_handle(sock)
    t.join(timeout=5)
    assert "frame" not in received, "no frames should flow after failed auth"
    print("  ok: loopback transport rejects a wrong token")



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
            # Placeholder: the real viewer address is captured from the
            # pairing request's source IP when the bundle is pulled.
            viewer_ip="198.51.100.7", viewer_port=9000, auth_token=auth_token,
            tls_cert=cert_path, tls_key=key_path,
        )
        cfg.validate()
        cfg_path = os.path.join(host_dir, "config.json")
        cfg.save(cfg_path)

        otp = os.urandom(crypto.OTP_SIZE)
        fp = crypto.cert_fingerprint(cert_path)
        pairing_port = 9001
        code = crypto.encode_pairing_code("127.0.0.1", pairing_port, otp, fp)

        with open(cert_path, "r") as fh:
            cert_pem = fh.read()
        with open(key_path, "r") as fh:
            key_pem = fh.read()
        bundle = {"cert_pem": cert_pem, "key_pem": key_pem,
                  "cert_fingerprint": setup_mod._b64e(fp),
                  "config": cfg.to_dict()}
        state = {"otp": otp, "bundle": bundle, "served": False,
                 "config_path": cfg_path, "viewer_ip_override": None}

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
        # The served config must carry the viewer's real address (the source
        # IP of this pull), not the placeholder from generation time...
        assert got["config"]["viewer_ip"] == "127.0.0.1"
        # ...and the host's config.json on disk is rewritten to match, since
        # the daemon dials this address.
        with open(cfg_path, "r") as fh:
            assert json.load(fh)["viewer_ip"] == "127.0.0.1"
        # The cert PEM stays cert-only; the key ships as its own field (the
        # viewer is the TLS server and must present the private key).
        assert "PRIVATE KEY" not in got["cert_pem"]
        assert "PRIVATE KEY" in got["key_pem"]

        httpd.shutdown()
        print("  ok: host->viewer pairing flow (bundle pulled, fp verified, viewer_ip captured, key shipped)")


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


def test_windows_config_path_precedence():
    """Service config resolution: env > install-time registry > repo default."""
    from daemon.service import windows as win_svc

    root = "/opt/remoteview"
    # Env wins over everything (and is made absolute).
    assert (
        win_svc._choose_config_path("/x/config.json", "/reg/config.json", root)
        == os.path.abspath("/x/config.json")
    )
    # Registry (persisted at install) beats the default.
    assert (
        win_svc._choose_config_path(None, "/reg/config.json", root)
        == "/reg/config.json"
    )
    # Nothing set -> <project root>/config.json.
    assert (
        win_svc._choose_config_path(None, None, root)
        == os.path.join(root, "config.json")
    )
    print("  ok: service config precedence (env > registry > repo default)")


def test_windows_relative_tls_paths_resolve_against_config_dir():
    """Relative cert/key paths must resolve next to config.json.

    The service CWD is C:\\Windows\\system32, so a relative tls_cert/tls_key in
    config.json would never be found without this normalization.
    """
    from daemon.service import windows as win_svc
    from shared.config import Config

    cfg = Config(tls_cert="certs/server.crt", tls_key="server.key")
    win_svc._apply_config_dir(cfg, "/etc/remoteview/config.json")
    assert cfg.tls_cert == "/etc/remoteview/certs/server.crt"
    assert cfg.tls_key == "/etc/remoteview/server.key"

    # Absolute paths are left untouched.
    abs_cert = os.path.abspath("/var/certs/x.crt")
    cfg2 = Config(tls_cert=abs_cert, tls_key="/var/certs/x.key")
    win_svc._apply_config_dir(cfg2, "/etc/remoteview/config.json")
    assert cfg2.tls_cert == abs_cert
    assert cfg2.tls_key == "/var/certs/x.key"
    print("  ok: relative tls paths resolve against the config directory")


def test_windows_module_puts_project_root_on_path():
    """windows.py must bootstrap the project root onto sys.path.

    pythonservice.exe imports the service file as top-level `windows` with only
    <repo>\\daemon\\service on sys.path; the lazy `shared.*`/`daemon.*` imports
    need the project root, which the module inserts itself.
    """
    from daemon.service import windows as win_svc

    assert win_svc._PROJECT_ROOT in sys.path
    assert os.path.isfile(os.path.join(win_svc._PROJECT_ROOT, "shared", "config.py"))
    print("  ok: windows.py bootstraps the project root onto sys.path")


def main():
    print("Running Phase 2 (service/setup) tests:")
    test_loopback_transport_roundtrip()
    test_loopback_transport_rejects_bad_token()
    test_render_unit()
    test_render_plist()
    test_windows_config_path_precedence()
    test_windows_relative_tls_paths_resolve_against_config_dir()
    test_windows_module_puts_project_root_on_path()
    test_host_viewer_pairing_flow()
    test_one_time_token_invalidated()
    print("\nAll Phase 2 tests passed.")


if __name__ == "__main__":
    main()
