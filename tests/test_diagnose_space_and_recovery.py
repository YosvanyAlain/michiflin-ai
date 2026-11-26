"""Synthetic-only tests: no production account, cloud or mailbox access."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

import state_backup as backup
from tools import diagnose_space_and_recovery as diagnostic


def make_state(root: Path) -> Path:
    folder = root / 'accounts' / 'one'
    blob = folder / 'dc.db-blobs'
    blob.mkdir(parents=True)
    (root / 'accounts' / 'accounts.toml').write_text('[[accounts]]\ndir = "one"\n')
    local = {'apps': {}, 'jobs': {'1': {'kind': 'file', 'url': 'https://example.invalid/file',
                                         'chat': 13, 'messages': [1, 3, 4]}}}
    (root / 'michiflin-state.json').write_text(json.dumps(local))
    marker = {'version': 1, 'confirmed': True, 'fresh_account': True, 'epoch': 'a' * 48,
              'owner_fingerprint': 'B' * 40, 'bot_fingerprint': 'C' * 40,
              'owner_address': 'owner@example.invalid', 'owner_chat': 10}
    (root / backup.CLEAN_OWNER_ORIGIN).write_text(json.dumps(marker))
    with sqlite3.connect(folder / 'dc.db') as db:
        db.execute('CREATE TABLE msgs (id INTEGER PRIMARY KEY, param TEXT, type INTEGER, from_id INTEGER, state INTEGER, deleted INTEGER, chat_id INTEGER)')
        rows = [('f=$BLOBDIR/private-app.xdc', 80, 1, 26, 0, 13),
                ('f=$BLOBDIR/private-book.pdf', 20, 12, 10, 0, 13),
                ('f=$BLOBDIR/archive.zip', 20, 1, 26, 0, 13),
                ('f=$BLOBDIR/inflight.bin', 20, 1, 20, 0, 13)]
        db.executemany('INSERT INTO msgs (param,type,from_id,state,deleted,chat_id) VALUES (?,?,?,?,?,?)', rows)
    for name, content in [('private-app.xdc', b'a' * 12), ('private-app-copy.xdc', b'a' * 12),
                          ('private-book.pdf', b'b' * 23), ('archive.zip', b'c' * 34),
                          ('inflight.bin', b'd' * 45), ('unclassified.bin', b'e' * 56)]:
        (blob / name).write_bytes(content)
    return root


def test_footprint_counts_categories_without_names(tmp_path):
    state = make_state(tmp_path / 'state')
    report = diagnostic.footprint(state)
    assert report['account_database_count'] == 1
    assert report['kind_webxdc_count'] == 2 and report['kind_webxdc_bytes'] == 24
    assert report['kind_document_count'] == 1 and report['kind_document_bytes'] == 23
    assert report['kind_archive_count'] == 1 and report['kind_archive_bytes'] == 34
    assert report['ref_webxdc_reference_bytes'] == 12
    assert report['ref_incoming_or_unknown_reference_bytes'] == 23
    assert report['ref_outgoing_delivered_only_reference_bytes'] == 34
    assert report['ref_outgoing_unsettled_reference_bytes'] == 45
    assert report['ref_no_direct_msg_reference_bytes'] == 68
    assert report['known_delivered_url_download_count'] == 1
    assert report['known_delivered_url_download_bytes'] == 34
    assert report['duplicate_blob_count'] == 1 and report['duplicate_blob_bytes'] == 12
    assert report['duplicate_webxdc_count'] == 1 and report['duplicate_webxdc_bytes'] == 12
    assert type(report['dbstat_available']) is bool
    assert 'private-book' not in json.dumps(report)
    assert all(type(item) in (int, bool) for item in report.values())


def test_download_candidates_fail_closed_on_shared_or_wrong_chat(tmp_path):
    state = make_state(tmp_path / 'state')
    db_path = state / 'accounts' / 'one' / 'dc.db'
    assert diagnostic.known_download_references(state, db_path) == {'archive.zip'}
    with sqlite3.connect(db_path) as db:
        db.execute('INSERT INTO msgs (param,type,from_id,state,deleted,chat_id) VALUES (?,?,?,?,?,?)',
                   ('f=$BLOBDIR/archive.zip', 20, 12, 10, 0, 13))
    assert diagnostic.known_download_references(state, db_path) == set()
    data = json.loads((state / 'michiflin-state.json').read_text())
    data['jobs']['1']['chat'] = 14
    (state / 'michiflin-state.json').write_text(json.dumps(data))
    assert diagnostic.known_download_references(state, db_path) == set()


def test_download_candidates_exclude_user_local_filekit(tmp_path):
    state = make_state(tmp_path / 'state')
    data = json.loads((state / 'michiflin-state.json').read_text())
    data['jobs']['1']['filekit_local'] = True
    (state / 'michiflin-state.json').write_text(json.dumps(data))
    assert diagnostic.known_download_references(state, state / 'accounts' / 'one' / 'dc.db') == set()


def test_footprint_fails_closed_for_symlinks(tmp_path):
    state = make_state(tmp_path / 'state')
    (state / 'unsafe-link').symlink_to(state / 'michiflin-state.json')
    with pytest.raises(ValueError):
        diagnostic.footprint(state)


def test_distinct_second_backup_decrypts_and_installs_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv('STATE_BACKUP_NAMESPACE', 'owner-clean-v1')
    monkeypatch.setenv('MICHIFLIN_CLEAN_EPOCH', 'a' * 48)
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    state = make_state(tmp_path / 'state')
    plaintext = backup.archive(state)
    salt = os.urandom(backup.SALT_BYTES)
    encrypted = backup.MAGIC + salt + backup.fernet_from('pass', salt).encrypt(plaintext)
    digest = hashlib.sha256(encrypted).hexdigest()
    prefix = backup.CLEAN_OWNER_ASSET_PREFIX
    primary = {'id': 1, 'size': 42, 'created_at': '2026-11-26T21:44:00Z', 'state': 'uploaded',
               'name': prefix + '20261126T214400Z-0000000000000001.bin', 'digest': 'sha256:' + 'd' * 64}
    second = {'id': 2, 'size': len(encrypted), 'created_at': '2026-11-26T21:43:00Z', 'state': 'uploaded',
              'name': prefix + '20261126T214300Z-0000000000000002.bin', 'digest': 'sha256:' + digest}
    trace = {'asset_id': 1, 'bytes': 42, 'sha256': 'd' * 64}
    monkeypatch.setattr(backup, 'configuration', lambda: ('token', 'owner/repo', 'pass'))
    monkeypatch.setattr(backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(backup, '_release', lambda *_: {'id': 9})
    monkeypatch.setattr(backup, '_assets', lambda *_: [primary, second])
    monkeypatch.setattr(backup, '_download_asset', lambda *_: encrypted)
    with mock.patch.object(backup.requests, 'delete') as delete:
        report = diagnostic.verify_second(state, trace)
    delete.assert_not_called()
    assert report['primary_asset_id'] == 1 and report['second_asset_id'] == 2
    assert report['second_decrypted_and_installed'] is True
    assert report['both_snapshots_in_retention_set'] is True
    assert not list(tmp_path.glob('verify-second-*'))
    primary_marker = state / backup.CLEAN_OWNER_ORIGIN
    changed_marker = json.loads(primary_marker.read_text())
    changed_marker['bot_fingerprint'] = 'D' * 40
    primary_marker.write_text(json.dumps(changed_marker))
    with pytest.raises(ValueError, match='identities_do_not_match'):
        diagnostic.verify_second(state, trace)
    assert not list(tmp_path.glob('verify-second-*'))
    second['digest'] = 'sha256:' + 'a' * 64
    with pytest.raises(ValueError, match='digest_mismatch'):
        diagnostic.verify_second(state, trace)
