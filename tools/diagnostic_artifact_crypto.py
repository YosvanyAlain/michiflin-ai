#!/usr/bin/env python3
"""Encrypt aggregate diagnostics for a caller-provided X25519 public key.

The workflow receives only a public key. Its matching private key must remain
outside the public repository and outside GitHub Actions. Outputs are bound
with ChaCha20-Poly1305 and contain no cleartext diagnostic values.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b'MICHIFLIN-DIAGNOSTIC-V1\0'
INFO = b'michiflin-actions-diagnostic-v1'
MAX_BYTES = 1_000_000


def _key_bytes(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) != 44:
        raise ValueError('invalid key length')
    try:
        value = base64.b64decode(encoded.encode('ascii'), altchars=b'-_', validate=True)
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError('invalid key encoding') from None
    if len(value) != 32:
        raise ValueError('invalid key length')
    return value


def validate_public_key(encoded: str) -> X25519PublicKey:
    public = X25519PublicKey.from_public_bytes(_key_bytes(encoded))
    # Reject low-order points, including the all-zero point, before any restore.
    X25519PrivateKey.generate().exchange(public)
    return public


def _derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)


def encrypt_blob(plaintext: bytes, public_key: str) -> bytes:
    if not isinstance(plaintext, bytes) or len(plaintext) > MAX_BYTES:
        raise ValueError('invalid diagnostic size')
    public = validate_public_key(public_key)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    aad = MAGIC + ephemeral_public
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(_derive(ephemeral.exchange(public))).encrypt(nonce, plaintext, aad)
    return aad + nonce + ciphertext


def decrypt_blob(blob: bytes, private_key: str) -> bytes:
    if not isinstance(blob, bytes) or not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 32 + 12 + 16:
        raise ValueError('invalid encrypted diagnostic')
    if len(blob) > MAX_BYTES + len(MAGIC) + 32 + 12 + 16:
        raise ValueError('encrypted diagnostic too large')
    private = X25519PrivateKey.from_private_bytes(_key_bytes(private_key))
    offset = len(MAGIC)
    ephemeral_public = blob[offset:offset + 32]
    nonce = blob[offset + 32:offset + 44]
    ciphertext = blob[offset + 44:]
    shared = private.exchange(X25519PublicKey.from_public_bytes(ephemeral_public))
    return ChaCha20Poly1305(_derive(shared)).decrypt(nonce, ciphertext, MAGIC + ephemeral_public)


def _read_source(source: Path) -> tuple[bytes, object]:
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise ValueError('invalid diagnostic source')
    data = source.read_bytes()
    return data, json.loads(data)


def _write_encrypted(data: bytes, destination: Path, public_key: str) -> None:
    encrypted = encrypt_blob(data, public_key)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(encrypted)
        output.flush()
        os.fsync(output.fileno())


def encrypt_aggregate(source: Path, destination: Path, public_key: str) -> None:
    data, value = _read_source(source)
    if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('read_only') is not True
            or not all(type(item) in (int, bool) or item is None for item in value.values())):
        raise ValueError('aggregate schema required')
    _write_encrypted(data, destination, public_key)


def encrypt_restore_trace(source: Path, destination: Path, public_key: str) -> None:
    """Encrypt only the exact asset identity, never arbitrary string fields."""
    data, value = _read_source(source)
    if (not isinstance(value, dict)
            or set(value) != {'schema', 'read_only', 'mail_io_started', 'asset_id', 'bytes', 'sha256'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['read_only'] is not True or value['mail_io_started'] is not False
            or type(value['asset_id']) is not int or value['asset_id'] <= 0
            or type(value['bytes']) is not int or not 0 < value['bytes'] <= 256_000_000
            or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])):
        raise ValueError('restore trace schema required')
    _write_encrypted(data, destination, public_key)


def main() -> int:
    try:
        public_key = os.environ.get('DIAGNOSTIC_PUBLIC_KEY', '')
        if sys.argv[1:] == ['validate']:
            validate_public_key(public_key)
            print('DIAGNOSTIC_ENCRYPTION_KEY_VALID')
            return 0
        if len(sys.argv) == 4 and sys.argv[1] in {'encrypt', 'encrypt-trace'}:
            function = encrypt_restore_trace if sys.argv[1] == 'encrypt-trace' else encrypt_aggregate
            function(Path(sys.argv[2]), Path(sys.argv[3]), public_key)
            print('DIAGNOSTIC_ENCRYPTED')
            return 0
    except Exception:
        # Error messages can contain paths or invalid input; print a fixed marker only.
        print('DIAGNOSTIC_ENCRYPTION_FAILED', file=sys.stderr)
        return 1
    print('Usage: diagnostic_artifact_crypto.py validate | encrypt|encrypt-trace SOURCE DESTINATION', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
