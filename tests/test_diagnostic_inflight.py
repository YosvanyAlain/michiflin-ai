"""Synthetic, read-only checkpoint inspection; never export message fields."""
import hashlib
import json
import sqlite3

from tools.diagnose_sync_snapshot import diagnose


def test_inflight_categories_are_aggregate_and_do_not_touch_the_database(tmp_path):
    jobs = {
        'delivered-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 101}, 'url': 'PRIVATE-URL'},
        'pending-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 102}},
        'failed-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 103}},
        'deleted-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 104}},
        'missing-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 199}},
        'other-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 105}},
        'wrong-chat-private': {'status': 'sending', 'chat': 7, 'inflight': {'msg_id': 106}},
        'unknown-chat-private': {'status': 'sending', 'inflight': {'msg_id': 107}},
        'unknown-id-private': {'status': 'sending', 'chat': 42, 'inflight': {'msg_id': 0}},
        'done-private': {'status': 'done', 'chat': 42, 'inflight': {'msg_id': 101}},
    }
    state = tmp_path / 'michiflin-state.json'
    state.write_text(json.dumps({'apps': {}, 'jobs': jobs}))
    root = tmp_path / 'accounts' / 'synthetic'
    root.mkdir(parents=True)
    database = root / 'dc.db'
    with sqlite3.connect(database) as db:
        db.executescript('''
          CREATE TABLE msgs(id INTEGER, type INTEGER, chat_id INTEGER, deleted INTEGER, param TEXT, state INTEGER);
          CREATE TABLE contacts(addr TEXT, fingerprint TEXT, verifier INTEGER);
          CREATE TABLE chats(id INTEGER);
          CREATE TABLE msgs_status_updates(id INTEGER);
          INSERT INTO msgs VALUES
            (101,10,42,0,'PRIVATE-TEXT',26), (102,10,42,0,'',18),
            (103,10,42,0,'',24), (104,10,42,1,'',26),
            (105,10,42,0,'',13), (106,10,42,0,'',26), (107,10,42,0,'',26);
        ''')
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = diagnose(tmp_path)
    assert result['queue_inflight_message_delivered'] == 1
    assert result['queue_inflight_message_pending'] == 1
    assert result['queue_inflight_message_failed'] == 1
    assert result['queue_inflight_message_missing_or_deleted'] == 2
    assert result['queue_inflight_message_other'] == 1
    assert result['queue_inflight_message_chat_mismatch'] == 1
    assert result['queue_inflight_message_chat_unknown'] == 1
    assert result['queue_active_inflight_without_message_id'] == 1
    assert result['mail_io_started'] is False
    encoded = json.dumps(result)
    for secret in ('PRIVATE-URL', 'PRIVATE-TEXT', 'delivered-private', 'wrong-chat-private'):
        assert secret not in encoded
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
