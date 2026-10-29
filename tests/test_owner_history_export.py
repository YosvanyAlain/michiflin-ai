"""Non-destructive owner-history separation; no old account credentials survive."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import io
import json
import sqlite3
import tarfile

import pytest

from michiflin import purge_users, state
from tools import export_owner_history_once as ex

OWNER = "owner@example.invalid"
OLD_SECRET = "old-password-DO-NOT-COPY-987654321"
OLD_KEY = "private-old-key-DO-NOT-COPY-123456789"
OLD_TOKEN = "old-invite-token-DO-NOT-COPY-123"


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "source"
    (path / "accounts").mkdir(parents=True)
    blob = path / "blob"
    blob.mkdir()
    (blob / "present.bin").write_bytes(b"owner attachment" * 50000)  # includes a >200 KB file
    database = path / "synthetic.db"
    with sqlite3.connect(database) as db:
        def table(name, columns):
            fields = ','.join('"' + x + '"' + (' INTEGER' if x in {'id', 'chat_id', 'msg_id'} else ' TEXT') for x in columns)
            db.execute('CREATE TABLE "' + name + '" (' + fields + ')')
        table("chats", ex.CHAT_COLUMNS)
        table("msgs", ex.MSG_COLUMNS)
        table("msgs_status_updates", ex.UPDATE_COLUMNS)
        db.execute('CREATE TABLE config (keyname TEXT, value TEXT)')
        db.execute('CREATE TABLE keypairs (private_key TEXT)')
        db.execute('CREATE TABLE tokens (token TEXT)')
        db.execute('CREATE TABLE transports (entered_param TEXT, configured_param TEXT)')
        db.executemany('INSERT INTO chats (id,name) VALUES (?,?)', [(12,'Owner'), (13,'Other')])
        db.executemany('INSERT INTO msgs (id,chat_id,txt,param) VALUES (?,?,?,?)', [
            (100,12,'Owner note with '+OLD_SECRET, 'f=$BLOBDIR/present.bin'),
            (101,12,'Another owner note', 'f=$BLOBDIR/missing.bin'),
            (102,13,'OTHER PERSON NOT EXPORTED', ''),
        ])
        db.executemany('INSERT INTO msgs_status_updates (id,msg_id,update_item,uid) VALUES (?,?,?,?)', [
            (200,100,'owner WebXDC update '+OLD_TOKEN,'u1'), (201,102,'OTHER PERSON UPDATE','u2')])
        db.execute('INSERT INTO keypairs VALUES (?)', (OLD_KEY,))
        db.execute('INSERT INTO config VALUES (?,?)', ('configured_mail_pw', OLD_SECRET))
        db.execute('INSERT INTO transports VALUES (?,?)', ('mail_pw='+OLD_SECRET, ''))
        db.execute('INSERT INTO tokens VALUES (?)', (OLD_TOKEN,))
    data = copy.deepcopy(state.data)
    data['notes'] = {OWNER:[{'text':'owner-only note'}], 'other@example.invalid':[{'text':'OTHER PERSON'}]}
    data['links'] = {'1': {'title':'owner saved link'}}
    (path / 'michiflin-state.json').write_text(json.dumps(data), encoding='utf-8')

    class Rpc:
        def get_config(self, _a, _key): return 'old-bot@example.invalid'
        def get_blob_dir(self, _a): return str(blob)
    @contextmanager
    def account_rpc(_path): yield Rpc()
    monkeypatch.setattr(ex.relay, 'account_rpc', account_rpc)
    monkeypatch.setattr(ex.relay, 'only_account', lambda _rpc: 1)
    monkeypatch.setattr(purge_users, '_classify', lambda *_args: ({13}, {12}, set()))
    monkeypatch.setattr(purge_users, '_db', lambda *_args: sqlite3.connect(database))
    return path, blob, database


def test_preserves_owner_messages_updates_and_available_blobs_without_old_identity(source):
    path, _blob, _db = source
    parts, counts = ex._build_parts(path, OWNER)
    assert counts == {'chats':1, 'messages':2, 'updates':1, 'attachments_present':1,
                      'attachments_missing':1, 'redacted_old_secrets':2}
    joined = b''.join(parts.values())
    assert b'owner attachment' in joined and b'owner-only note' in joined and b'owner WebXDC update' in joined
    assert b'OTHER PERSON' not in joined and OLD_KEY.encode() not in joined and OLD_SECRET.encode() not in joined and OLD_TOKEN.encode() not in joined
    assert b'missing.bin' in joined and b'credencial de identidad anterior retirada' in joined
    raw = ex.pack(parts, counts)
    assert ex.verify(raw, expected=parts, expected_counts=counts)['counts'] == counts
    assert len([name for name in parts if name.startswith('archivos/')]) == 1
    assert path.joinpath('accounts').is_dir()  # no mutation/deletion of old source


def test_identity_key_found_in_attachment_aborts_without_deleting_anything(source):
    path, blob, _db = source
    (blob / 'present.bin').write_bytes(OLD_KEY.encode())
    with pytest.raises(ex.ExportError, match='attachment'):
        ex._build_parts(path, OWNER)
    assert (blob / 'present.bin').read_bytes() == OLD_KEY.encode()


def test_symlink_attachment_is_treated_as_missing(source):
    path, blob, _db = source
    (blob / 'present.bin').unlink()
    (blob / 'present.bin').symlink_to('/etc/passwd')
    _parts, counts = ex._build_parts(path, OWNER)
    assert counts['attachments_present'] == 0 and counts['attachments_missing'] == 2


def test_tampered_archive_is_rejected(source):
    path, _blob, _db = source
    parts, counts = ex._build_parts(path, OWNER)
    raw = ex.pack(parts, counts)
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
        data = {x.name: tar.extractfile(x).read() for x in tar.getmembers()}
    data['mensajes.json'] = b'[]'  # leave the original manifest intact
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        for name, payload in data.items():
            info = tarfile.TarInfo(name); info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(ex.ExportError, match='mismatch'):
        ex.verify(out.getvalue())


def test_rows_are_chunked_without_missing_records():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE msgs (id INTEGER, chat_id INTEGER, param TEXT)')
        db.executemany('INSERT INTO msgs VALUES (?,?,?)', [(i,i,'') for i in range(1,1201)])
        rows = ex._rows(db, 'msgs', ('id','param'), 'chat_id', set(range(1,1201)))
    assert len(rows) == 1200 and {x['id'] for x in rows} == set(range(1,1201))


def test_recovery_uses_a_new_private_directory_and_does_not_recreate_account(source, tmp_path):
    from tools import restore_owner_history as restore
    path, _blob, _db = source
    parts, counts = ex._build_parts(path, OWNER)
    raw = ex.pack(parts, counts)
    target = tmp_path / 'recovered'
    restore.recover(raw, target)
    assert json.loads((target / 'mensajes.json').read_text())
    assert not (target / 'accounts').exists() and not (target / 'dc.db').exists()
    assert (target.stat().st_mode & 0o777) == 0o700
    assert ((target / 'mensajes.json').stat().st_mode & 0o777) == 0o600
    with pytest.raises(ex.ExportError, match='already exists'):
        restore.recover(raw, target)


def test_historical_versions_and_old_only_attachments_are_preserved_without_duplicates(source):
    path, blob, database = source
    first, first_counts = ex._build_parts(path, OWNER)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO msgs (id,chat_id,txt,param) VALUES (103,12,'older owner message','f=$BLOBDIR/older.bin')")
        db.execute("INSERT INTO msgs_status_updates (id,msg_id,update_item,uid) VALUES (202,103,'older update','u3')")
    (blob / 'older.bin').write_bytes(b'older attachment')
    earlier, earlier_counts = ex._build_parts(path, OWNER)
    merged, counts = ex.merge_historical_parts(first, first_counts, [(earlier, earlier_counts), (earlier, earlier_counts)])
    index = json.loads(merged['indice-historico.json'])
    assert len(index['revisiones']) == counts['source_versions'] == 3
    older_messages = merged[index['revisiones'][1]['documentos']['mensajes.json']]
    assert b'older owner message' in older_messages
    assert counts['attachment_versions'] == 2 and b'older attachment' in b''.join(merged.values())
    raw = ex.pack(merged, counts)
    assert ex.verify(raw, expected=merged, expected_counts=counts)['counts']['source_versions'] == 3


def test_cli_recovers_downloaded_encrypted_archive_offline(source, tmp_path, monkeypatch):
    from tools import cutover_identity_once as cutover, restore_owner_history as restore
    path, _blob, _db = source
    parts, counts = ex._build_parts(path, OWNER)
    encrypted = cutover.seal_bytes(ex.pack(parts, counts), 'private backup passphrase for fixture')
    filename = tmp_path / 'archive.enc'
    filename.write_bytes(encrypted)
    monkeypatch.setenv('STATE_BACKUP_PASSPHRASE', 'private backup passphrase for fixture')
    assert restore.main([str(filename), '--check']) == 0
    target = tmp_path / 'restored'
    assert restore.main([str(filename), '--destino', str(target)]) == 0
    assert (target / 'mensajes.json').is_file()
    monkeypatch.setenv('STATE_BACKUP_PASSPHRASE', 'wrong passphrase')
    assert restore.main([str(filename), '--check']) == 1


def test_existing_archive_must_cover_every_source_and_attachment(source):
    path, blob, database = source
    latest, counts = ex._build_parts(path, OWNER)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO msgs (id,chat_id,txt,param) VALUES (103,12,'older message','f=$BLOBDIR/older.bin')")
    (blob / 'older.bin').write_bytes(b'older attachment')
    older, old_counts = ex._build_parts(path, OWNER)
    merged, total = ex.merge_historical_parts(latest, counts, [(older, old_counts)])
    raw = ex.pack(merged, total)
    assert ex.verify_sources_covered(raw, [(latest, counts), (older, old_counts)])['counts']['source_versions'] == 2
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO msgs (id,chat_id,txt) VALUES (104,12,'unarchived message')")
    new, new_counts = ex._build_parts(path, OWNER)
    with pytest.raises(ex.ExportError, match='not covered'):
        ex.verify_sources_covered(raw, [(new, new_counts)])
