"""Ed25519 signing for license keys and activations.

Token format (must match app/plannerboard/license/client.py):

    <base64url(payload_json)>.<base64url(signature)>

payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
The signature covers exactly those bytes.

Private key: PEM (PKCS8) at $LIC_PRIVATE_KEY_PATH, default
license_server/keys/DEV_ONLY_private.pem. Generate with scripts/gen_keys.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PKG_DIR = Path(__file__).resolve().parent
DEFAULT_PRIVATE_KEY_PATH = PKG_DIR.parent / "keys" / "DEV_ONLY_private.pem"


class SigningError(Exception):
    pass


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def private_key_path() -> Path:
    return Path(os.environ.get("LIC_PRIVATE_KEY_PATH") or DEFAULT_PRIVATE_KEY_PATH)


def load_private_key(path: str | os.PathLike | None = None) -> Ed25519PrivateKey:
    p = Path(path) if path else private_key_path()
    if not p.exists():
        raise SigningError(f"private key not found at {p}; run scripts/gen_keys.py or set LIC_PRIVATE_KEY_PATH")
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("private key is not Ed25519")
    return key


@lru_cache(maxsize=4)
def _cached_private(path_str: str) -> Ed25519PrivateKey:
    return load_private_key(path_str)


class Signer:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(Path(path) if path else private_key_path())
        self._key = _cached_private(self.path)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    def key_id(self) -> str:
        """Short fingerprint of the public key, handy for rotation checks."""
        raw = self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return hashlib.sha256(raw).hexdigest()[:16]

    def sign_payload(self, payload: dict) -> str:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = self._key.sign(payload_bytes)
        return f"{b64u_encode(payload_bytes)}.{b64u_encode(sig)}"

    def verify(self, token: str) -> dict:
        return verify_token(token, self.public_key)


def verify_token(token: str, public_key: Ed25519PublicKey) -> dict:
    token = (token or "").strip().replace("\n", "").replace(" ", "")
    if token.count(".") != 1:
        raise SigningError("malformed token")
    p, s = token.split(".", 1)
    try:
        payload_bytes, sig = b64u_decode(p), b64u_decode(s)
    except (ValueError, TypeError) as exc:
        raise SigningError(f"malformed token: {exc}") from exc
    try:
        public_key.verify(sig, payload_bytes)
    except InvalidSignature as exc:
        raise SigningError("bad signature") from exc
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SigningError("payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise SigningError("payload is not an object")
    return payload


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_pem, public_pem) for a fresh Ed25519 pair."""
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub
