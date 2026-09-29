"""VAPID key management for Web Push.

Keys are generated once (EC P-256) and persisted under the instance data
directory (``~/.nanobot/push/vapid.json``) with 0600 permissions.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from nanobot.config.paths import get_runtime_subdir

_VAPID_DIR_NAME = "push"
_VAPID_FILE_NAME = "vapid.json"
_lock = threading.Lock()
_cached: "VapidKeys | None" = None


@dataclass(frozen=True)
class VapidKeys:
    """A VAPID key pair.

    ``public_key`` is the url-safe base64 encoding of the raw public key
    point (the form browsers expect in ``pushManager.subscribe``).
    ``private_key_pem`` is the PEM-encoded private key used to sign
    push claims.
    """

    public_key: str
    private_key_pem: str
    public_key_pem: str


def _default_vapid_path() -> Path:
    return get_runtime_subdir(_VAPID_DIR_NAME) / _VAPID_FILE_NAME


def _generate_vapid_keys() -> VapidKeys:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()
    raw_public = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    public_key = base64.urlsafe_b64encode(raw_public).rstrip(b"=").decode("ascii")
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return VapidKeys(
        public_key=public_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".vapid-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_or_create_vapid_keys(path: Path | None = None) -> VapidKeys:
    """Load VAPID keys from disk, generating and persisting them if absent."""
    global _cached
    target = path or _default_vapid_path()
    with _lock:
        if path is None and _cached is not None:
            return _cached
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
                keys = VapidKeys(
                    public_key=str(raw["public_key"]),
                    private_key_pem=str(raw["private_key_pem"]),
                    public_key_pem=str(raw.get("public_key_pem", "")),
                )
                if path is None:
                    _cached = keys
                return keys
            except (ValueError, KeyError, TypeError):
                # Corrupt file: regenerate rather than fail startup.
                pass
        keys = _generate_vapid_keys()
        payload = json.dumps(
            {
                "public_key": keys.public_key,
                "private_key_pem": keys.private_key_pem,
                "public_key_pem": keys.public_key_pem,
            },
            indent=2,
        ).encode("utf-8")
        _write_atomic(target, payload)
        if path is None:
            _cached = keys
        return keys
