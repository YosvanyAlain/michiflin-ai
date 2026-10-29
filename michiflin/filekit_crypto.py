"""Ephemeral envelopes for sensitive file data carried by WebXDC updates.

Only ciphertext is sent in an update. The X25519 private key lives in this bot
process and is discarded on restart; request/session guards reject stale work.
"""
from __future__ import annotations

import base64
import hmac
import secrets
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .filekit import FileKitError

_INFO = b"michiflin-filekit-password-v1"
_REPLY = b"michiflin-filekit-reply-v1"
_PRIVATE = X25519PrivateKey.generate()
_KEY_ID = secrets.token_urlsafe(16)


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def public_key() -> dict[str, str]:
    public = _PRIVATE.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"keyId": _KEY_ID, "publicKey": _encode(public)}


def _decrypt(envelope: Any, max_bytes: int) -> str:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("keyId"), str) or not hmac.compare_digest(envelope["keyId"], _KEY_ID):
        raise FileKitError("secret_expired")
    try:
        values = [envelope.get(key) for key in ("ephemeral", "nonce", "ciphertext")]
        if any(not isinstance(value, str) for value in values) or len(values[0]) > 64 or len(values[1]) > 32 or len(values[2]) > (max_bytes + 32) * 2:
            raise ValueError("invalid envelope")
        ephemeral, nonce, ciphertext = (base64.b64decode(value, validate=True) for value in values)
        if len(ephemeral) != 32 or len(nonce) != 12 or not 16 <= len(ciphertext) <= max_bytes + 16:
            raise ValueError("invalid envelope")
        peer = X25519PublicKey.from_public_bytes(ephemeral)
        shared = _PRIVATE.exchange(peer)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=_INFO).derive(shared)
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, _INFO + b"|" + _KEY_ID.encode()).decode("utf-8")
    except Exception:
        # Do not propagate parser or crypto exceptions that could echo data.
        raise FileKitError("secret_invalid") from None


def decrypt(envelope: Any) -> str:
    text = _decrypt(envelope, 4096)
    if len(text) > 1024 or any(char in text for char in "\x00\r\n"):
        raise FileKitError("invalid_password")
    return text


def decrypt_text(envelope: Any) -> str:
    return _decrypt(envelope, 70000)


def encrypt_reply(public_key_b64: Any, data: bytes) -> dict[str, str]:
    """Encrypt one response to an ephemeral key held only by the requesting UI."""
    try:
        if not isinstance(public_key_b64, str) or len(public_key_b64) != 44 or not isinstance(data, bytes) or len(data) > 300000:
            raise ValueError("invalid reply key or data")
        peer_bytes = base64.b64decode(public_key_b64, validate=True)
        if len(peer_bytes) != 32:
            raise ValueError("invalid reply key")
        peer = X25519PublicKey.from_public_bytes(peer_bytes)
        private = X25519PrivateKey.generate()
        ephemeral = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        shared = private.exchange(peer)
        nonce = secrets.token_bytes(12)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=_REPLY).derive(shared)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, data, _REPLY + b"|" + public_key_b64.encode("ascii"))
        return {"ephemeral": _encode(ephemeral), "nonce": _encode(nonce), "ciphertext": _encode(ciphertext)}
    except Exception:
        raise FileKitError("secret_invalid") from None
