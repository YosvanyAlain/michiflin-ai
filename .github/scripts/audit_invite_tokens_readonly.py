"""Inspect only the isolated test contact in the current clean snapshot.

No bot process or network identity is started. The authenticated backup and
its database remain in memory. Only the known synthetic contact and its own chat metadata are queried.
No user conversations, message text, addresses or keys are logged or exported.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import io
import json
import subprocess
import tempfile
import os
import re
import sqlite3
import sys
import tarfile
import time
from pathlib import PurePosixPath

import requests
from cryptography.fernet import Fernet

MAGIC = b'MICHIFLIN-STATE-1\0'
PATTERN = re.compile(r'^michiflin-owner-clean-state-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin$')


def main():
    token = os.environ['STATE_TOKEN']
    repo = os.environ['STATE_REPO']
    password = os.environ['STATE_PASSPHRASE']
    api = 'https://api.github.com/repos/' + repo
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'}
    listing = requests.get(api + '/releases/tags/michiflin-state-releases-v1', headers=headers, timeout=45)
    listing.raise_for_status()
    assets = [a for a in listing.json().get('assets', []) if PATTERN.fullmatch(a.get('name', ''))]
    if not assets:
        raise RuntimeError('no clean snapshot')
    asset = max(assets, key=lambda a: (a.get('created_at', ''), a.get('id', 0)))
    if not 0 < int(asset['size']) <= 90_000_000:
        raise ValueError('snapshot size')
    headers['Accept'] = 'application/octet-stream'
    response = requests.get(asset['url'], headers=headers, timeout=120)
    response.raise_for_status()
    ciphertext = response.content
    if not ciphertext.startswith(MAGIC) or len(ciphertext) != int(asset['size']):
        raise ValueError('invalid ciphertext')
    salt = ciphertext[len(MAGIC):len(MAGIC) + 16]
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 480000, dklen=32)
    archive = Fernet(base64.urlsafe_b64encode(key)).decrypt(ciphertext[len(MAGIC) + 16:])
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tf:
        databases = [m for m in tf.getmembers() if m.isfile() and PurePosixPath(m.name).name == 'dc.db']
        if len(databases) != 1 or databases[0].size > 80_000_000:
            raise ValueError('unexpected account count')
        handle = tf.extractfile(databases[0])
        if handle is None:
            raise ValueError('database missing')
        database_bytes = handle.read()
        markers = [m for m in tf.getmembers() if m.isfile() and PurePosixPath(m.name).name == '.owner-clean-origin-v1.json']
        if len(markers)!=1 or markers[0].size > 4096:
            raise ValueError('clean identity marker unavailable')
        marker_handle = tf.extractfile(markers[0])
        if marker_handle is None:
            raise ValueError('clean identity marker missing')
        marker = json.load(marker_handle)
        states = [m for m in tf.getmembers() if m.isfile() and m.name == 'michiflin/michiflin-state.json']
        if len(states)!=1 or states[0].size>10000000:
            raise ValueError('state metadata unavailable')
        state_handle = tf.extractfile(states[0])
        if state_handle is None:
            raise ValueError('state metadata missing')
        state = json.load(state_handle)
    # Memory-only SQLite: only identity metadata and SecureJoin tokens are queried.
    db = sqlite3.connect(':memory:')
    db.deserialize(database_bytes)
    db.execute('PRAGMA query_only = ON')
    test = json.loads(os.environ['TEST_CONTACT'])
    address = str(test['address']).strip().casefold()
    challenge = base64.b64decode(test['challenge'], validate=True)
    if not re.fullmatch(r'[^\s@]{1,128}@[^\s@]{1,253}',address) or len(challenge)!=32:
        raise ValueError('invalid test-contact selector')
    target = db.execute('SELECT id, fingerprint, verifier, blocked FROM contacts WHERE lower(addr)=? AND id>9',(address,)).fetchone()
    result = {'TEST_CONTACT_PRESENT': bool(target)}
    if target:
        cid, fingerprint, verifier, blocked = target
        fp = str(fingerprint or '').replace(' ','').upper()
        result['TEST_KEY_MATCH'] = hmac.compare_digest(hmac.new(challenge,fp.encode(),hashlib.sha256).hexdigest(),test['fingerprint'])
        result['TEST_VERIFIED_BY_SELF'] = int(verifier or 0)==1
        result['TEST_CORE_BLOCKED'] = bool(blocked)
        chats = db.execute('SELECT chats.id, chats.blocked, chats.type FROM chats JOIN chats_contacts ON chats.id=chats_contacts.chat_id WHERE chats_contacts.contact_id=? AND chats.type=100',(cid,)).fetchall()
        result['TEST_SINGLE_CHAT_PRESENT'] = bool(chats)
        result['TEST_CHAT_UNBLOCKED'] = any(int(row[1] or 0)==0 for row in chats)
        ids = [int(row[0]) for row in chats]
        # Read only this synthetic contact's new message metadata, never its text.
        count=0
        for chat_id in ids:
            count += db.execute('SELECT COUNT(*) FROM msgs WHERE chat_id=? AND from_id=? AND hidden=0 AND deleted=0 AND timestamp>=?',(chat_id,cid,1794166380)).fetchone()[0]
        print('SAFE_TEST_INCOMING_AFTER_1933_COUNT='+str(count))
        access = state.get('access',{}) if isinstance(state,dict) else {}
        blocked_list = access.get('blocked',[]) if isinstance(access,dict) else []
        result['TEST_APP_BLOCKED'] = any(str(item).casefold()==address for item in blocked_list) if isinstance(blocked_list,list) else True
        apps = state.get('apps',{}) if isinstance(state,dict) else {}
        app_count = sum(1 for entry in apps.values() if isinstance(entry,dict) and str(entry.get('chat','')).isdigit() and int(entry['chat']) in ids) if isinstance(apps,dict) else 0
        print('SAFE_TEST_APP_INSTANCE_COUNT='+str(app_count))
    for name,value in result.items():
        print('SAFE_'+name+'='+('YES' if value else 'NO'))
    print('SAFE_SNAPSHOT_CREATED='+str(asset.get('created_at','unknown')))
    db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('SAFE_INVITE_AUDIT_ERROR=' + type(exc).__name__)
        sys.exit(1)
