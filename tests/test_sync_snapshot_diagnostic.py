"""No real account, secrets or mail I/O: synthetic read-only diagnostic data."""
import hashlib
import json
import sqlite3

from tools.diagnose_sync_snapshot import diagnose


def test_diagnostic_is_readonly_and_does_not_emit_private_fields(tmp_path, monkeypatch):
    marker = {'epoch': 'test-epoch', 'owner_address': 'owner@example.invalid',
              'owner_chat': 42, 'owner_fingerprint': 'A' * 40}
    (tmp_path / '.owner-clean-origin-v1.json').write_text(json.dumps(marker))
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', 'b' * 40)
    data = {'apps': {'100': {'chat': 42, 'addr': marker['owner_address'],
                            'build': 'c8a5f423ba', 'epoch': 'test-epoch', 'recovery_revision': 'b' * 40},
                     '101': {'chat': 42, 'addr': marker['owner_address'],
                             'build': 'unknown', 'epoch': 'different'}},
            'owner_refresh_intents': {'b' * 40: {'state': 'reserved', 'epoch': 'test-epoch'}}}
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
    assert result['requested_refresh_reserved'] == 1
    assert result['requested_refresh_registered'] == 1
    assert result['requested_refresh_delivered'] == 1
    assert result['requested_refresh_pending'] == 0
    assert result['requested_refresh_failed'] == 0
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


def test_diagnostic_honors_exact_previously_shipped_builds(tmp_path, monkeypatch):
    from michiflin import app_compat
    monkeypatch.setenv('REVOKE_LEGACY_MINIAPPS', '1')
    monkeypatch.setenv('MINIAPP_RELEASE', 'synthetic-release')
    monkeypatch.setenv('BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(app_compat, 'community_previous_build', lambda *args: '')
    monkeypatch.setattr(app_compat, 'shipped_ui_build', lambda epoch, release, build: epoch == 'synthetic-epoch' and release == 'synthetic-release' and build == 'previously-shipped')
    data = {'apps': {'100': {'build': 'previously-shipped', 'epoch': 'synthetic-epoch', 'chat': 42},
                     '101': {'build': 'unknown', 'epoch': 'synthetic-epoch', 'chat': 42}}}
    (tmp_path / 'michiflin-state.json').write_text(json.dumps(data))
    root = tmp_path / 'accounts' / 'synthetic'; root.mkdir(parents=True)
    with sqlite3.connect(root / 'dc.db') as db:
        db.executescript("CREATE TABLE msgs(id INTEGER,type INTEGER,chat_id INTEGER,deleted INTEGER,param TEXT,state INTEGER); CREATE TABLE contacts(addr TEXT,fingerprint TEXT,verifier INTEGER); CREATE TABLE chats(id INTEGER); CREATE TABLE msgs_status_updates(id INTEGER);")
    assert diagnose(tmp_path)['apps_incompatible_build'] == 1


def test_current_only_diagnostic_uses_packaged_build_and_checks_epoch(tmp_path, monkeypatch):
    from michiflin import config, miniapp
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'synthetic-release')
    monkeypatch.setenv('BOT_EPOCH', 'synthetic-epoch')
    monkeypatch.setenv('MINIAPP_RELEASE', 'synthetic-release')
    monkeypatch.setenv('MINIAPP_CURRENT_ONLY', 'true')
    monkeypatch.setenv('REVOKE_LEGACY_MINIAPPS', '0')
    build = miniapp.app_build()
    apps = {
        '100': {'build': build, 'epoch': 'synthetic-epoch', 'chat': 42},
        '101': {'build': 'older-build', 'epoch': 'synthetic-epoch', 'chat': 42},
        '102': {'build': build, 'epoch': 'different-epoch', 'chat': 42},
    }
    (tmp_path / 'michiflin-state.json').write_text(json.dumps({'apps': apps}))
    root = tmp_path / 'accounts' / 'synthetic'; root.mkdir(parents=True)
    with sqlite3.connect(root / 'dc.db') as db:
        db.executescript("""
          CREATE TABLE msgs(id INTEGER,type INTEGER,chat_id INTEGER,deleted INTEGER,param TEXT,state INTEGER);
          CREATE TABLE contacts(addr TEXT,fingerprint TEXT,verifier INTEGER);
          CREATE TABLE chats(id INTEGER);
          CREATE TABLE msgs_status_updates(id INTEGER);
          INSERT INTO msgs VALUES (100,80,42,0,'',26), (101,80,42,0,'',26), (102,80,42,0,'',26);
        """)
    result = diagnose(tmp_path)
    assert result['revocation_enabled'] and result['current_only_enabled']
    assert result['current_release_bindings'] == 1
    assert result['apps_incompatible_build'] == 2
    assert result['apps_core_present'] == 3


def test_diagnostic_revision_input_cannot_arm_the_production_workflow():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    tests_workflow = (root / '.github/workflows/tests.yml').read_text()
    production = (root / '.github/workflows/michiflin.yml').read_text()
    assert 'diagnostic_revision:' in tests_workflow
    assert "inputs.diagnostic_revision || vars.OWNER_REQUESTED_APP_REVISION" in tests_workflow
    assert 'diagnostic_revision' not in production
