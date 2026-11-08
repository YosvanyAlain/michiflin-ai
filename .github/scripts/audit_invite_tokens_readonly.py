"""Prepare an encrypted invitation capsule from clean-state token metadata.

No bot process or network identity is started. The authenticated backup and
its database remain in memory. No contacts, message tables or blobs are queried. Addresses, tokens and
fingerprints are not logged. The capsule goes only to the private state release.
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
from nacl.public import PublicKey, SealedBox

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
    # Memory-only SQLite: only the SecureJoin token namespace is queried.
    db = sqlite3.connect(':memory:')
    db.deserialize(database_bytes)
    db.execute('PRAGMA query_only = ON')
    invite_row = db.execute("SELECT token FROM tokens WHERE namespc=100 AND (foreign_key='' OR foreign_key IS NULL) ORDER BY id DESC LIMIT 1").fetchone()
    auth_row = db.execute("SELECT token, timestamp FROM tokens WHERE namespc=110 AND (foreign_key='' OR foreign_key IS NULL) ORDER BY id DESC LIMIT 1").fetchone()
    address_row = db.execute("SELECT value FROM config WHERE keyname='configured_addr'").fetchone()
    public_row = db.execute("SELECT public_key FROM keypairs WHERE id=(SELECT value FROM config WHERE keyname='key_id')").fetchone()
    if not invite_row or not auth_row or not address_row or not public_row:
        raise ValueError('required contact metadata missing')
    if not 0 <= int(time.time()) - int(auth_row[1]) < 6 * 86400:
        raise ValueError('no sufficiently fresh auth token')
    public_key = bytes(public_row[0])
    with tempfile.TemporaryDirectory(prefix='michiflin-public-') as home:
        result = subprocess.run(['gpg','--no-options','--batch','--no-autostart','--no-auto-key-retrieve',
                                 '--homedir',home,'--with-colons','--show-keys'],
                                input=public_key,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=True)
    primary = []
    waiting = False
    for line in result.stdout.decode('utf-8','strict').splitlines():
        fields = line.split(':'); kind = fields[0]
        if kind in ('sec','ssb'):
            raise ValueError('not public key material')
        if kind == 'pub':
            primary.append(''); waiting = True
        elif kind in ('sub','uid','uat'):
            waiting = False
        elif kind == 'fpr' and waiting and len(fields)>9:
            primary[-1] = fields[9]; waiting = False
    if len(primary)!=1 or not re.fullmatch(r'[A-Fa-f0-9]{40,64}', primary[0]):
        raise ValueError('public fingerprint unavailable')
    payload = json.dumps({'fingerprint':primary[0], 'address':address_row[0],
                          'invite':invite_row[0], 'auth':auth_row[0],
                          'auth_timestamp':int(auth_row[1]),
                          'snapshot_created':asset.get('created_at','unknown')}, separators=(',',':')).encode()
    recipient = PublicKey(base64.b64decode(os.environ['CAPSULE_PUBLIC_KEY'], validate=True))
    capsule = SealedBox(recipient).encrypt(payload)
    upload_url = listing.json()['upload_url'].split('{',1)[0]
    if not upload_url.startswith('https://uploads.github.com/repos/' + repo + '/'):
        raise ValueError('unexpected upload host')
    name = 'temporary-secure-check-' + os.environ['GITHUB_RUN_ID'] + '.bin'
    if any(a.get('name') == name for a in listing.json().get('assets', [])):
        raise ValueError('capsule already exists')
    upload_headers = {**headers, 'Content-Type':'application/octet-stream', 'Accept':'application/vnd.github+json'}
    upload = requests.post(upload_url, headers=upload_headers, params={'name':name}, data=capsule, timeout=45)
    upload.raise_for_status()
    uploaded = upload.json()
    if uploaded.get('name')!=name or not isinstance(uploaded.get('id'),int):
        raise ValueError('capsule upload unconfirmed')
    print('SAFE_CAPSULE_ASSET_ID=' + str(uploaded['id']))
    print('SAFE_CAPSULE_ENCRYPTED=YES')
    print('SAFE_SNAPSHOT_CREATED=' + str(asset.get('created_at','unknown')))
    db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('SAFE_INVITE_AUDIT_ERROR=' + type(exc).__name__)
        sys.exit(1)
