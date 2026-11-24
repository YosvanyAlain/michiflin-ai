"""Real Delta Chat Core with a disposable, artificially seeded profile.

No account is configured and IO is never started. This checks the .xdc archive
and local status-update API, NOT delivery by mail or a native client UI.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from unittest import mock

import pytest
from deltachat2 import IOTransport, Rpc
from deltachat2.types import MessageData, Viewtype

from michiflin import config, miniapp


def test_actual_xdc_and_chunked_updates_round_trip_through_local_delta_core(tmp_path, monkeypatch):
    executable = shutil.which('deltachat-rpc-server')
    if not executable:
        adjacent = Path(sys.executable).parent / 'deltachat-rpc-server'
        executable = str(adjacent) if adjacent.is_file() else None
    if not executable:
        pytest.skip('Delta Chat RPC server not installed')
    root = tmp_path / 'profile'
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path / 'package')
    package = miniapp.build()
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        account = rpc.add_account()
        rpc.set_config(account, 'addr', 'offline-test@example.invalid')
    db = next(root.glob('*/dc.db'))
    # A throwaway local profile has no mail transports or keys. Seed only the
    # minimum metadata needed by this pinned Core version; no IO is started.
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT OR REPLACE INTO config(keyname,value) VALUES('configured_addr','offline-test@example.invalid')")
        connection.execute("INSERT INTO removed_transports(addr,remove_timestamp) VALUES('retired-synthetic@example.invalid',1)")
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        chat_id = rpc.create_chat_by_contact_id(account, 1)
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO msgs(chat_id,from_id,to_id,timestamp,state,type,txt,rfc724_mid) "
                           "VALUES(?,1,1,1,10,10,'offline-seed','offline-seed@example.invalid')", (chat_id,))
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        message_id = rpc.send_msg(account, chat_id, MessageData(file=str(package), filename='synthetic.xdc', viewtype=Viewtype.WEBXDC))
        info = rpc.get_webxdc_info(account, message_id)
        assert info.send_update_max_size >= 128_000
        encoded_asset = rpc.get_webxdc_blob(account, message_id, 'vendor/books/pdf.worker.min.mjs')
        asset = base64.b64decode(encoded_asset + '=' * (-len(encoded_asset) % 4), altchars=b'-_', validate=True)
        assert asset == (miniapp.APP_DIR / 'vendor/books/pdf.worker.min.mjs').read_bytes()
        raw = (b'Original synthetic data.\n' * 40_000)[:900_000]
        data = {'format': 'txt', 'data': base64.b64encode(raw).decode(), 'index': 0, 'count': 1}
        sizes = []
        def send(_instance, payload, *_args, **_kwargs):
            encoded = json.dumps({'payload': payload}, ensure_ascii=False, separators=(',', ':'))
            sizes.append(len(encoded.encode()))
            rpc.send_webxdc_status_update(account, message_id, encoded, '')
            return True
        with mock.patch.object(miniapp, '_answers', {}), mock.patch.object(miniapp, '_status_allowed', return_value=True), \
             mock.patch.object(miniapp, '_send_status', side_effect=send):
            miniapp.respond(message_id, 'synthetic-request', data)
        received = json.loads(rpc.get_webxdc_status_updates(account, message_id, 0))
        chunks = [item['payload'] for item in received if item.get('payload', {}).get('re') == 'synthetic-request']
        assert len(chunks) > 50 and all(item['t'] == 'chunk' for item in chunks)
        assert [item['i'] for item in chunks] == list(range(len(chunks)))
        assert all(item['n'] == len(chunks) for item in chunks)
        assert all(size < 128_000 for size in sizes)
        response = json.loads(''.join(item['s'] for item in chunks))
        assert response['ok'] is True and base64.b64decode(response['data']['data']) == raw
