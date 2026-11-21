"""No real account, secrets or mail I/O: synthetic read-only diagnostic data."""
import hashlib
import json
import sqlite3

from tools.diagnose_sync_snapshot import diagnose


def test_diagnostic_is_readonly_and_does_not_emit_private_fields(tmp_path, monkeypatch):
    marker = {'epoch': 'test-epoch', 'owner_address': 'owner@example.invalid',
              'owner_chat': 42, 'owner_fingerprint': 'A' * 40}
    (tmp_path / '.owner-clean-origin-v1.json').write_text(json.dumps(marker))
    data = {'apps': {'100': {'chat': 42, 'addr': marker['owner_address'],
                            'build': 'c8a5f423ba', 'epoch': 'test-epoch'},
                     '101': {'chat': 42, 'addr': marker['owner_address'],
                             'build': 'unknown', 'epoch': 'different'}}}
    (tmp_path / 'michiflin-state.json').write_text(json.dumps(data))
    root = tmp_path / 'accounts' / 'synthetic'
    root.mkdir(parents=True)
    path = root / 'dc.db'
    with sqlite3.connect(path) as db:
        db.executescript('''
          CREATE TABLE msgs(id INTEGER, type INTEGER, chat_id INTEGER, deleted INTEGER, param TEXT, state INTEGER);
          CREATE TABLE contacts(addr TEXT, fingerprint TEXT, verifier INTEGER);
          CREATE TABLE chats(id INTEGER);
          CREATE TABLE msgs_status_updates(id INTEGER);
          INSERT INTO msgs VALUES(100,80,42,0,'PRIVATE-PARAMETERS',26),(101,80,42,1,'',26),(102,80,42,0,'',20);
          INSERT INTO chats VALUES(42);
        ''')
        db.execute('INSERT INTO contacts VALUES(?,?,1)', (marker['owner_address'], 'A' * 40))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = diagnose(tmp_path)
    assert result['app_bindings'] == 2
    assert result['apps_core_present'] == 1
    assert result['apps_core_missing_or_deleted'] == 1
    assert result['unregistered_core_webxdc'] == 1
    assert result['apps_incompatible_build'] == 1
    assert result['apps_other_epoch'] == 1
    assert result['pending_outgoing'] == 1
    assert result['owner_marker_fingerprint_present'] is True
    assert result['owner_directly_verified_contact_present'] is True
    assert result['mail_io_started'] is False
    assert all(isinstance(v, (int, bool)) or v is None for v in result.values())
    serialized = json.dumps(result)
    for secret in ['owner@example.invalid', 'PRIVATE-PARAMETERS', 'A' * 40, 'c8a5f423ba']:
        assert secret not in serialized
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    monkeypatch.setenv('REVOKE_LEGACY_MINIAPPS', '1')
    monkeypatch.setenv('MINIAPP_RELEASE', 'synthetic-new-generation')
    strict = diagnose(tmp_path)
    assert strict['revocation_enabled'] is True
    assert strict['current_release_bindings'] == 0
    assert strict['apps_incompatible_build'] == 2
    assert strict['release_opening_checkpointed'] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_ambiguous_database_is_not_silently_selected(tmp_path):
    (tmp_path / 'michiflin-state.json').write_text('{"apps":{}}')
    result = diagnose(tmp_path)
    assert result['database_count'] == 0
    assert 'database_quick_check_ok' not in result
