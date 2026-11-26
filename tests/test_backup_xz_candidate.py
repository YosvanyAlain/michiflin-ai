"""Offline tests for a reversible optional compression; no real accounts or network."""
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
import state_backup as backup


def synthetic_state(root: Path) -> None:
    account = root / 'accounts' / 'one'
    blobs = account / 'dc.db-blobs'
    blobs.mkdir(parents=True)
    (root / 'accounts' / 'accounts.toml').write_text('[[accounts]]\ndir = "one"\n')
    (root / 'michiflin-state.json').write_text(json.dumps({'version': 5, 'jobs': {'1': {'status': 'sending'}}}))
    (root / 'identity.asc').write_bytes(b'synthetic private identity')
    with sqlite3.connect(account / 'dc.db') as db:
        db.execute('CREATE TABLE data (id INTEGER, item TEXT)')
        db.execute('INSERT INTO data VALUES (1, ?)', ('keep this content',))
    (blobs / 'first.xdc').write_bytes(b'repeated-unchanged-content' * 1000 + b'first')
    (blobs / 'second.xdc').write_bytes(b'repeated-unchanged-content' * 1000 + b'second')
    (blobs / 'personal.png').write_bytes(b'photo remains')


def test_xz_preserves_all_bytes_identity_and_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv('STATE_BACKUP_COMPRESSION', 'xz')
    monkeypatch.setenv('STATE_BACKUP_INCLUDE_BLOBS', '1')
    monkeypatch.setenv('STATE_BACKUP_INCLUDE_MEDIA', '1')
    source = tmp_path / 'source'
    synthetic_state(source)
    archive = backup.archive(source)
    assert archive.startswith(b'\xfd7zXZ\x00')
    backup._verify_plaintext(archive)
    target = tmp_path / 'restored'
    backup._install_restored(archive, target)
    for relative in ['identity.asc', 'michiflin-state.json', 'accounts/accounts.toml',
                     'accounts/one/dc.db-blobs/first.xdc', 'accounts/one/dc.db-blobs/second.xdc',
                     'accounts/one/dc.db-blobs/personal.png']:
        assert (target / relative).read_bytes() == (source / relative).read_bytes()
    with sqlite3.connect(target / 'accounts' / 'one' / 'dc.db') as db:
        assert db.execute('SELECT * FROM data').fetchall() == [(1, 'keep this content')]
        assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
    # The old reader rejects this format instead of silently restoring a partial account.
    with pytest.raises(tarfile.ReadError):
        tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz')


def test_new_reader_still_restores_original_gzip(monkeypatch, tmp_path):
    monkeypatch.delenv('STATE_BACKUP_COMPRESSION', raising=False)
    monkeypatch.setenv('STATE_BACKUP_INCLUDE_BLOBS', '1')
    source = tmp_path / 'source'
    synthetic_state(source)
    archive = backup.archive(source)
    assert archive.startswith(b'\x1f\x8b')
    backup._verify_plaintext(archive)
    backup._install_restored(archive, tmp_path / 'restored')
    assert (tmp_path / 'restored' / 'identity.asc').read_bytes() == b'synthetic private identity'


def test_unknown_compression_fails_without_upload(monkeypatch, tmp_path):
    monkeypatch.setenv('STATE_BACKUP_COMPRESSION', 'typo')
    synthetic_state(tmp_path / 'state')
    with pytest.raises(ValueError, match='previous backups were kept'):
        backup.archive(tmp_path / 'state')
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        archive.addfile(tarfile.TarInfo('michiflin'))
    with pytest.raises(ValueError, match='unknown backup archive'):
        backup.safely_extract(output.getvalue(), tmp_path / 'restore')


@pytest.mark.parametrize('mode', ['w:gz', 'w:xz'])
def test_both_compressions_still_reject_traversal_and_symlinks(tmp_path, mode):
    for name, link in [('../escape', False), ('michiflin/link', True)]:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode=mode) as archive:
            info = tarfile.TarInfo(name)
            if link:
                info.type = tarfile.SYMTYPE
                info.linkname = '../../escape'
            archive.addfile(info)
        with pytest.raises(ValueError):
            backup.safely_extract(output.getvalue(), tmp_path / 'restore')
