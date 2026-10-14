"""RemoteView crypto helpers: cert generation, TLS contexts, token verify,
and pairing-code encode/decode.

Security properties (from the design doc):
  * TLS 1.3 enforced, no fallback to lower versions.
  * Viewer pins the self-signed cert -- rejects any other cert on reconnect.
  * Auth token verified with a constant-time compare before any frame flows.

The private key never leaves the home (daemon) machine.
"""

from __future__ import annotations

import base64
import datetime
import hmac
import ipaddress
import os
import ssl
import struct
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# --- Pairing code format (see design doc "Phase 1 -- Pairing") ---
# base32( ip_bytes(4) + port_uint16(2) + one_time_token_16bytes + fingerprint_4bytes )
# Displayed as "RVEW-XXXX-XXXX-...".
PAIRING_PREFIX = "RVEW"
PAIRING_MAGIC = b"RVEW1"  # magic + version byte, guards against random input
OTP_SIZE = 16             # one-time token size (bytes)
FINGERPRINT_SIZE = 4      # truncated cert fingerprint size (bytes)

__all__ = [
    "generate_cert_and_key",
    "cert_fingerprint",
    "build_ssl_context_server",
    "build_ssl_context_client",
    "verify_token",
    "generate_auth_token",
    "encode_pairing_code",
    "decode_pairing_code",
    "PAIRING_PREFIX",
]


# --------------------------------------------------------------------------- #
# Certificate generation
# --------------------------------------------------------------------------- #
def generate_cert_and_key(
    cert_path: str,
    key_path: str,
    common_name: str = "RemoteView",
    days_valid: int = 3650,
    key_size: int = 2048,
) -> Tuple[str, str]:
    """Generate a self-signed cert + RSA private key and write them to disk.

    Returns ``(cert_path, key_path)``. The key file is written with 0600 perms.
    The cert is used purely for encryption + pinning (the viewer pins this
    exact cert per the design doc).
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)

    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)

    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass  # best-effort on non-POSIX
    return cert_path, key_path


def cert_fingerprint(cert_path: str, size: int = FINGERPRINT_SIZE) -> bytes:
    """Return a short fingerprint (SHA-256 prefix) of a PEM cert.

    Used to bind a pairing code to a specific cert so the viewer can confirm
    the cert it downloads during pairing is the expected one.
    """
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    digest = cert.fingerprint(hashes.SHA256())
    return digest[:size]


# --------------------------------------------------------------------------- #
# TLS contexts
# --------------------------------------------------------------------------- #
def build_ssl_context_server(cert: str, key: str) -> ssl.SSLContext:
    """Build a TLS 1.3 **server** context (used by the viewer's listener).

    Loads the pinned cert + key. TLS 1.3 is enforced (no lower fallback).
    Client cert verification is not required -- the app-level HMAC token
    performs authentication after the handshake.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def build_ssl_context_client(cert: str) -> ssl.SSLContext:
    """Build a TLS 1.3 **client** context that pins the given self-signed cert.

    Used by the daemon, which dials out to the viewer. The daemon must reject
    any cert other than the pinned one -- we load the cert as the trust anchor
    and require verification, so a mismatch aborts the handshake.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False

    # Load the pinned cert as the only trusted CA. Because the cert is
    # self-signed, requiring verification against this anchor rejects any
    # other cert presented on the wire.
    ctx.load_verify_locations(cafile=cert)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


# --------------------------------------------------------------------------- #
# Auth token
# --------------------------------------------------------------------------- #
def generate_auth_token() -> str:
    """Generate a fresh 32-byte auth token as a 64-char hex string."""
    return os.urandom(32).hex()


def verify_token(received: bytes, expected_hex: str) -> bool:
    """Constant-time compare of a received token against the expected hex.

    ``received`` is the raw bytes off the socket; ``expected_hex`` is the
    64-char hex string from config. Returns True only on an exact match.
    """
    if received is None:
        return False
    try:
        expected = bytes.fromhex(expected_hex)
    except (ValueError, TypeError):
        return False
    if len(expected) != len(received):
        return False
    return hmac.compare_digest(received, expected)



# --------------------------------------------------------------------------- #
# Pairing code: base32( magic + ip(4) + port(2) + otp(16) + fingerprint(4) )
# --------------------------------------------------------------------------- #
def _pack_ip(ip: str) -> bytes:
    return ipaddress.ip_address(ip).packed  # IPv4 -> 4 bytes


def encode_pairing_code(
    ip: str, port: int, one_time_token: bytes, cert_fp: bytes
) -> str:
    """Encode a pairing payload into a displayable "RVEW-XXXX-..." code.

    ``ip`` must be IPv4 (packed to 4 bytes). ``one_time_token`` is 16 bytes and
    ``cert_fp`` is a (truncated) fingerprint of at least ``FINGERPRINT_SIZE``
    bytes. The result is base32 without padding, grouped in 4-char blocks.
    """
    if len(one_time_token) != OTP_SIZE:
        raise ValueError(f"one_time_token must be {OTP_SIZE} bytes")
    if len(cert_fp) < FINGERPRINT_SIZE:
        raise ValueError(f"cert_fp must be >= {FINGERPRINT_SIZE} bytes")

    ip_bytes = _pack_ip(ip)
    if len(ip_bytes) != 4:
        raise ValueError("only IPv4 addresses are supported in the pairing code")

    payload = (
        PAIRING_MAGIC
        + ip_bytes
        + struct.pack(">H", port)
        + one_time_token
        + cert_fp[:FINGERPRINT_SIZE]
    )
    b32 = base64.b32encode(payload).decode("ascii").rstrip("=")

    # Group into 4-char blocks and prefix with the magic label.
    blocks = [b32[i : i + 4] for i in range(0, len(b32), 4)]
    return PAIRING_PREFIX + "-" + "-".join(blocks)


def decode_pairing_code(code: str) -> dict:
    """Decode a pairing code back into its components.

    Returns a dict with keys: ip, port, otp (16 bytes), fingerprint (4 bytes).
    Raises ``ValueError`` on a malformed / foreign code.
    """
    if not code:
        raise ValueError("empty pairing code")
    cleaned = code.strip().upper()
    if cleaned.startswith(PAIRING_PREFIX + "-"):
        cleaned = cleaned[len(PAIRING_PREFIX) + 1 :]
    b32 = cleaned.replace("-", "")
    # Re-add base32 padding.
    pad = (-len(b32)) % 8
    try:
        payload = base64.b32decode(b32 + ("=" * pad))
    except Exception as exc:  # binascii.Error and friends
        raise ValueError(f"invalid pairing code encoding: {exc}") from exc

    expected = len(PAIRING_MAGIC) + 4 + 2 + OTP_SIZE + FINGERPRINT_SIZE
    if len(payload) != expected:
        raise ValueError(
            f"unexpected pairing payload length: {len(payload)} != {expected}"
        )
    if payload[: len(PAIRING_MAGIC)] != PAIRING_MAGIC:
        raise ValueError("pairing code magic mismatch (not a RemoteView code)")

    offset = len(PAIRING_MAGIC)
    ip_bytes = payload[offset : offset + 4]
    offset += 4
    (port,) = struct.unpack_from(">H", payload, offset)
    offset += 2
    otp = payload[offset : offset + OTP_SIZE]
    offset += OTP_SIZE
    fingerprint = payload[offset : offset + FINGERPRINT_SIZE]

    return {
        "ip": str(ipaddress.ip_address(ip_bytes)),
        "port": port,
        "otp": otp,
        "fingerprint": fingerprint,
    }

