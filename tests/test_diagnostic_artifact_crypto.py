"""Only synthetic material: verify authenticated diagnostic artifact encryption."""
import base64
import json
import os
import stat

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools import diagnostic_artifact_crypto as crypto


def _pair():
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return (base64.urlsafe_b64encode(raw_private).decode(), base64.urlsafe_b64encode(raw_public).decode())


def test_authenticated_roundtrip_randomized_and_tamper_rejected():
    private, public = _pair()
    plain = b'{"schema":1,"read_only":true,"synthetic_private_count":3}'
    first = crypto.encrypt_blob(plain, public)
    second = crypto.encrypt_blob(plain, public)
    assert first != second
    assert plain not in first and b'synthetic_private_count' not in first
    assert crypto.decrypt_blob(first, private) == plain
    wrong_private, _ = _pair()
    with pytest.raises(InvalidTag):
        crypto.decrypt_blob(first, wrong_private)
    damaged = first[:-1] + bytes([first[-1] ^ 1])
    with pytest.raises(InvalidTag):
        crypto.decrypt_blob(damaged, private)
    with pytest.raises(ValueError):
        crypto.decrypt_blob(b'not an artifact', private)


def test_invalid_and_low_order_keys_fail_before_restoration(monkeypatch, capsys):
    for value in ('', 'not-a-key', base64.urlsafe_b64encode(b'\0' * 32).decode()):
        monkeypatch.setenv('DIAGNOSTIC_PUBLIC_KEY', value)
        monkeypatch.setattr('sys.argv', ['diagnostic_artifact_crypto.py', 'validate'])
        assert crypto.main() == 1
    out = capsys.readouterr()
    assert 'DIAGNOSTIC_ENCRYPTION_FAILED' in out.err
    assert 'not-a-key' not in out.err


def test_only_fixed_schema_is_encrypted_and_existing_destination_is_preserved(tmp_path):
    private, public = _pair()
    source = tmp_path / 'counts.json'
    dest = tmp_path / 'counts.enc'
    original = {'schema': 1, 'read_only': True, 'pending': 2, 'unknown': None}
    source.write_text(json.dumps(original))
    crypto.encrypt_aggregate(source, dest, public)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert json.loads(crypto.decrypt_blob(dest.read_bytes(), private)) == original
    first = dest.read_bytes()
    with pytest.raises(FileExistsError):
        crypto.encrypt_aggregate(source, dest, public)
    assert dest.read_bytes() == first
    source.write_text(json.dumps({'schema': 1, 'read_only': True, 'name': 'private text'}))
    with pytest.raises(ValueError):
        crypto.encrypt_aggregate(source, tmp_path / 'rejected.enc', public)
    assert not (tmp_path / 'rejected.enc').exists()


def test_symlink_source_is_rejected(tmp_path):
    _private, public = _pair()
    source = tmp_path / 'source.json'
    source.write_text('{"schema":1,"read_only":true}')
    link = tmp_path / 'source-link'
    os.symlink(source, link)
    with pytest.raises(ValueError):
        crypto.encrypt_aggregate(link, tmp_path / 'out.enc', public)
