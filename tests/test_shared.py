"""Tests for the shared/ modules: protocol, config, and crypto.

Run with:  python -m pytest tests/ -v     (or)   python tests/test_shared.py
"""

import io
import os
import socket
import ssl
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import protocol as P  # noqa: E402
from shared.config import Config, ConfigError  # noqa: E402
from shared import crypto as C  # noqa: E402


# ----------------------------- protocol.py -------------------------------- #
def test_pack_unpack_roundtrip():
    payload = b"\xff\xd8\xff\xe0" + os.urandom(1000)
    ts = 1_700_000_000_123
    buf = P.pack_frame(payload, ts)

    (size,) = struct.unpack_from(">I", buf, 0)
    assert size == len(payload)
    total = P.SIZE_FIELD_SIZE + len(payload) + P.TIMESTAMP_FIELD_SIZE
    assert len(buf) == total

    out_payload, out_ts = P.unpack_frame(buf)
    assert out_payload == payload
    assert out_ts == ts

    size2, ts2 = P.unpack_frame_header(buf)
    assert size2 == len(payload)
    assert ts2 == ts
    print("  ok: protocol pack/unpack roundtrip")


def test_read_frame_from_stream():
    frames = [(b"AAAA", 111), (os.urandom(5000), 222), (b"BBBBBBBBBBBB", 333)]
    wire = b"".join(P.pack_frame(p, t) for p, t in frames)
    got = list(P.iter_frames(io.BytesIO(wire)))
    assert len(got) == len(frames)
    for (p, t), (gp, gt) in zip(frames, got):
        assert gp == p
        assert gt == t
    print("  ok: protocol read_frame over chunked stream")


def test_read_frame_clean_eof_returns_none():
    assert P.read_frame(io.BytesIO(b"")) is None
    partial = P.pack_frame(b"hello", 1)[:3]
    assert P.read_frame(io.BytesIO(partial)) is None
    print("  ok: protocol clean/partial EOF -> None")


def test_read_frame_rejects_oversize():
    bad = struct.pack(">I", P.MAX_FRAME_SIZE + 1)
    try:
        P.read_frame(io.BytesIO(bad))
        assert False, "should have raised"
    except ValueError:
        pass
    print("  ok: protocol rejects oversized length prefix")


# ------------------------------ config.py --------------------------------- #
def _valid_config_dict():
    return {
        "viewer_ip": "203.0.113.10",
        "viewer_port": 9000,
        "auth_token": "ab" * 32,
        "tls_cert": "certs/server.crt",
        "tls_key": "certs/server.key",
        "frame_rate": 10,
        "quality": 60,
        "monitor": 0,
    }


def test_config_load_and_validate():
    import json
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        with open(path, "w") as fh:
            json.dump(_valid_config_dict(), fh)
        cfg = Config.load(path)
        assert cfg.viewer_ip == "203.0.113.10"
        assert cfg.viewer_port == 9000
        assert cfg.frame_rate == 10
    print("  ok: config load + validate")


def test_config_missing_field_raises():
    import json
    bad = _valid_config_dict()
    del bad["auth_token"]
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.json")
        with open(path, "w") as fh:
            json.dump(bad, fh)
        try:
            Config.load(path)
            assert False, "should have raised"
        except ConfigError:
            pass
    print("  ok: config missing field raises ConfigError")


def test_config_range_validation():
    cfg = Config(viewer_ip="1.2.3.4", viewer_port=99999, auth_token="ab" * 32)
    try:
        cfg.validate()
        assert False
    except ConfigError:
        pass
    print("  ok: config range validation")


# ------------------------------- crypto.py -------------------------------- #
def test_token_generate_and_verify():
    hex_tok = C.generate_auth_token()
    assert len(hex_tok) == 64
    raw = bytes.fromhex(hex_tok)
    assert C.verify_token(raw, hex_tok) is True
    assert C.verify_token(b"\x00" * 32, hex_tok) is False
    assert C.verify_token(raw[:16], hex_tok) is False
    assert C.verify_token(None, hex_tok) is False
    print("  ok: token generate + constant-time verify")


def test_pairing_code_roundtrip():
    ip = "203.0.113.10"
    port = 9001
    otp = os.urandom(C.OTP_SIZE)
    fp = os.urandom(C.FINGERPRINT_SIZE)
    code = C.encode_pairing_code(ip, port, otp, fp)
    assert code.startswith("RVEW-")
    decoded = C.decode_pairing_code(code)
    assert decoded["ip"] == ip
    assert decoded["port"] == port
    assert decoded["otp"] == otp
    assert decoded["fingerprint"] == fp
    print(f"  ok: pairing code roundtrip ({code[:20]}...)")


def test_pairing_code_rejects_garbage():
    try:
        C.decode_pairing_code("AAAA-BBBB")
        assert False, "magic mismatch should raise"
    except ValueError:
        pass
    print("  ok: pairing code rejects foreign/garbage input")


def test_tls_contexts_and_handshake():
    import threading
    with tempfile.TemporaryDirectory() as d:
        cert = os.path.join(d, "server.crt")
        key = os.path.join(d, "server.key")
        C.generate_cert_and_key(cert, key, common_name="remoteview-test")
        fp = C.cert_fingerprint(cert)
        assert len(fp) == C.FINGERPRINT_SIZE

        server_ctx = C.build_ssl_context_server(cert, key)
        client_ctx = C.build_ssl_context_client(cert)
        assert server_ctx.minimum_version == ssl.TLSVersion.TLSv1_3

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        result = {}

        def run_server():
            conn, _ = srv.accept()
            tls = server_ctx.wrap_socket(conn, server_side=True)
            result["server_version"] = tls.version()
            result["server_got"] = tls.recv(16)
            tls.sendall(b"pong")
            tls.close()

        t = threading.Thread(target=run_server)
        t.start()

        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        tls_c = client_ctx.wrap_socket(raw, server_hostname="remoteview-test")
        tls_c.sendall(b"ping")
        result["client_version"] = tls_c.version()
        result["client_got"] = tls_c.recv(16)
        tls_c.close()
        t.join()
        srv.close()

        assert result["server_got"] == b"ping"
        assert result["client_got"] == b"pong"
        assert result["client_version"] == "TLSv1.3"
        assert result["server_version"] == "TLSv1.3"
    print("  ok: real TLS 1.3 loopback handshake (client pins cert)")


def test_client_rejects_wrong_cert():
    import threading
    with tempfile.TemporaryDirectory() as d:
        cert_a = os.path.join(d, "a.crt")
        key_a = os.path.join(d, "a.key")
        cert_b = os.path.join(d, "b.crt")
        key_b = os.path.join(d, "b.key")
        C.generate_cert_and_key(cert_a, key_a, common_name="A")
        C.generate_cert_and_key(cert_b, key_b, common_name="B")

        client_ctx = C.build_ssl_context_client(cert_a)  # pins A
        server_ctx = C.build_ssl_context_server(cert_b, key_b)  # presents B

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def run_server():
            try:
                conn, _ = srv.accept()
                tls = server_ctx.wrap_socket(conn, server_side=True)
                tls.close()
            except Exception:
                pass

        t = threading.Thread(target=run_server)
        t.start()
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        raised = False
        try:
            client_ctx.wrap_socket(raw, server_hostname="B")
        except ssl.SSLError:
            raised = True
        finally:
            raw.close()
            t.join()
            srv.close()
        assert raised, "client must reject a cert it did not pin"
    print("  ok: client rejects non-pinned cert (pinning works)")


def main():
    print("Running shared/ module tests:")
    test_pack_unpack_roundtrip()
    test_read_frame_from_stream()
    test_read_frame_clean_eof_returns_none()
    test_read_frame_rejects_oversize()
    test_config_load_and_validate()
    test_config_missing_field_raises()
    test_config_range_validation()
    test_token_generate_and_verify()
    test_pairing_code_roundtrip()
    test_pairing_code_rejects_garbage()
    test_tls_contexts_and_handshake()
    test_client_rejects_wrong_cert()
    print("\nAll shared tests passed.")


if __name__ == "__main__":
    main()

