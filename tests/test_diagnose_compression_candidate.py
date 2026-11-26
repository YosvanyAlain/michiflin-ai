"""Synthetic-only rehearsal tests; never connect to cloud or mail."""
import json
import os
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet

import state_backup as backup
from tools import diagnose_compression_candidate as d


def make_state(root: Path) -> Path:
    folder = root / 'accounts' / 'one'
    folder.mkdir(parents=True)
    (root / 'accounts' / 'accounts.toml').write_text('[[accounts]]\ndir = "one"\n')
    (root / 'michiflin-state.json').write_text('{"jobs":{},"apps":{}}')
    (root / backup.CLEAN_OWNER_ORIGIN).write_text(json.dumps({
        'version': 1, 'confirmed': True, 'fresh_account': True, 'epoch': 'a' * 48,
        'owner_fingerprint': 'B' * 40, 'bot_fingerprint': 'C' * 40,
        'owner_address': 'owner@example.invalid', 'owner_chat': 10}))
    with sqlite3.connect(folder / 'dc.db') as db:
        for name in d.CORE_TABLES:
            db.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, value BLOB)')
            db.execute(f'INSERT INTO "{name}" VALUES (1, ?)', (b'synthetic value',))
    blob = folder / 'dc.db-blobs'
    blob.mkdir()
    (blob / 'one.xdc').write_bytes(b'example bundle one')
    (blob / 'two.xdc').write_bytes(b'example bundle two')
    return root


def test_exact_fernet_encoded_size():
    for size in (0, 1, 15, 16, 17, 31, 32, 1024):
        encrypted = backup.MAGIC + b's' * backup.SALT_BYTES + Fernet(Fernet.generate_key()).encrypt(b'x' * size)
        assert d.encrypted_size(size) == len(encrypted)


def test_rehearsal_is_lossless_and_changes_no_source(monkeypatch, tmp_path):
    monkeypatch.setenv('MICHIFLIN_CLEAN_EPOCH', 'a' * 48)
    state = make_state(tmp_path / 'source')
    before = (state / 'michiflin-state.json').read_bytes()
    report = d.rehearsal(state, tmp_path, 1234)
    assert report['all_selected_nondatabase_files_identical'] is True
    assert report['core_database_tables_identical'] is True
    assert report['legacy_gzip_only_reader_rejects_new_format'] is True
    assert report['xz_would_fit'] is True
    assert (state / 'michiflin-state.json').read_bytes() == before
    assert not list(tmp_path.glob('compression-rehearsal-*'))
    assert 'STATE_BACKUP_COMPRESSION' not in os.environ


def test_expected_tmp_omission_is_reported_separately(monkeypatch, tmp_path):
    monkeypatch.setenv('MICHIFLIN_CLEAN_EPOCH', 'a' * 48)
    state = make_state(tmp_path / 'source')
    (state / 'scratch.tmp').write_text('temporary')
    report = d.rehearsal(state, tmp_path, 1234)
    assert report['all_selected_nondatabase_files_identical'] is False
    assert report['missing_file_count'] == 1
    assert report['missing_transient_sidecar_or_tmp_count'] == 1
    assert report['all_durable_nondatabase_contents_identical'] is True
    assert report['all_durable_nondatabase_modes_identical'] is True
    assert (state / 'scratch.tmp').exists()  # the rehearsal did not delete it


def test_fingerprint_detects_modified_or_missing_database_rows(tmp_path):
    state = make_state(tmp_path / 'source')
    db_path = state / 'accounts' / 'one' / 'dc.db'
    before = d.database_fingerprint(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute('UPDATE msgs SET value=?', (b'changed',))
    assert d.database_fingerprint(db_path) != before
