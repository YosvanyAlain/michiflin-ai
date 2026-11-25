"""Local Delta Chat Core probe: no mail IO, network or native client UI."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from deltachat2 import IOTransport, Rpc

from michiflin import chat, reader, state


def test_reader_chat_message_preserves_html_and_fallback_text_in_offline_core(tmp_path, monkeypatch):
    executable = shutil.which('deltachat-rpc-server')
    if not executable:
        adjacent = Path(sys.executable).parent / 'deltachat-rpc-server'
        executable = str(adjacent) if adjacent.is_file() else None
    if not executable:
        pytest.skip('Delta Chat RPC server not installed')
    root = tmp_path / 'profile'
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        account = rpc.add_account()
        rpc.set_config(account, 'addr', 'synthetic@example.invalid')
    db = next(root.glob('*/dc.db'))
    # An unconfigured disposable profile needs these synthetic rows in this
    # Core version. This does not reproduce an actual configured account.
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT OR REPLACE INTO config(keyname,value) VALUES('configured_addr','synthetic@example.invalid')")
        connection.execute("INSERT INTO removed_transports(addr,remove_timestamp) VALUES('retired@example.invalid',1)")
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        chat_id = rpc.create_chat_by_contact_id(account, 1)
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO msgs(chat_id,from_id,to_id,timestamp,state,type,txt,rfc724_mid) "
                           "VALUES(?,1,1,1,10,10,'seed','seed@example.invalid')", (chat_id,))
    page = reader.Page('https://fixture.example/story', title='Historia sintética',
                       blocks=['<p>Texto legible.</p>', '<figure><img src="data:image/jpeg;base64,AA==" alt="Imagen">'
                               '<figcaption>Pie de imagen</figcaption></figure>'])
    monkeypatch.setattr(state, 'setting', lambda key: 30)
    monkeypatch.setattr(state, 'link_id', lambda _url: 'fixture')
    monkeypatch.setattr(state, 'check_work_allowed', lambda: None)
    document, summary, _ = reader.build(page, 1, 'es')
    with IOTransport(str(root), rpc_executable=executable, stderr=subprocess.DEVNULL) as transport:
        rpc = Rpc(transport)
        monkeypatch.setattr(chat, '_bot', SimpleNamespace(rpc=rpc))
        monkeypatch.setattr(chat, '_accid', account)
        monkeypatch.setattr(chat, '_recent_out', deque(maxlen=60))
        message = chat.send(chat_id, summary, html=document)
        assert message
        assert rpc.get_message(account, message).text == summary
        assert rpc.get_message_html(account, message) == document
        assert '<figure>' in document and 'Pie de imagen' in document
