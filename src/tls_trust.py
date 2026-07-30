"""Trust-on-first-use certificate pinning for local printer services."""

import hashlib
import hmac
import json
import os
import socket
import ssl
import tempfile
import threading

_TRUST_LOCK = threading.Lock()


def _trust_path():
    configured = os.environ.get("FARM_TLS_TRUST")
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "tls_fingerprints.json")
    )


def _load_trust(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_trust(path, trust):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tls-trust-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(trust, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def verify_peer_certificate(host, port, certificate, expected_fingerprint=None):
    """Verify and pin the certificate from the connection that will carry credentials."""
    if not certificate:
        raise ssl.SSLError(f"No TLS certificate received from {host}:{port}")

    fingerprint = hashlib.sha256(certificate).hexdigest()
    expected = str(expected_fingerprint or "").lower().replace(":", "").strip()
    if expected:
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("Explicit TLS fingerprint must be 64 hexadecimal characters")
        if not hmac.compare_digest(fingerprint, expected):
            raise ssl.SSLError(f"TLS certificate fingerprint mismatch for {host}:{port}")
        return fingerprint

    key = f"{host}:{int(port)}"
    path = _trust_path()
    with _TRUST_LOCK:
        trust = _load_trust(path)
        pinned = str(trust.get(key, "")).lower()
        if pinned and not hmac.compare_digest(fingerprint, pinned):
            raise ssl.SSLError(f"Pinned TLS certificate changed for {key}")
        if not pinned:
            trust[key] = fingerprint
            _save_trust(path, trust)
    return fingerprint


def verify_tofu_certificate(host, port, expected_fingerprint=None, timeout=5):
    """Open a probe connection and verify its certificate without sending credentials."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, int(port)), timeout=timeout) as raw_socket,
        context.wrap_socket(raw_socket, server_hostname=host) as tls_socket,
    ):
        certificate = tls_socket.getpeercert(binary_form=True)
    return verify_peer_certificate(
        host,
        port,
        certificate,
        expected_fingerprint,
    )
