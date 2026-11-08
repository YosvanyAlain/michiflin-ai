"""Compare identity and invitation commitments against the clean snapshot.

No bot process or network identity is started. The authenticated backup and
its database remain in memory. No contacts, message tables or blobs are queried. Addresses, tokens and
fingerprints are not logged or exported.
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
    # Memory-only SQLite: only identity metadata and SecureJoin tokens are queried.
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
    commitment = json.loads(os.environ['IDENTITY_COMMITMENT'])
    challenge = base64.b64decode(commitment['challenge'], validate=True)
    if len(challenge)!=32:
        raise ValueError('invalid comparison challenge')
    def match(name, value):
        digest = hmac.new(challenge, str(value).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, commitment[name])
    token_rows = db.execute("SELECT namespc, token, timestamp FROM tokens WHERE namespc IN (100,110) AND (foreign_key='' OR foreign_key IS NULL) LIMIT 500").fetchall()
    invite_present = any(namespace==100 and match('invite', value) for namespace,value,_ in token_rows)
    auth_present = any(namespace==110 and match('auth', value) for namespace,value,_ in token_rows)
    matched_auth_fresh = any(namespace==110 and match('auth',value) and 0 <= int(time.time())-int(timestamp) < 6*86400 for namespace,value,timestamp in token_rows)
    marker_fp = str(marker.get('bot_fingerprint','')).replace(' ','').upper()
    results = {
        'CANDIDATE_ADDRESS_MATCH': match('address',str(address_row[0]).casefold()),
        'CANDIDATE_KEY_MATCH': match('fingerprint',primary[0].upper()),
        'CANDIDATE_MARKER_MATCH': match('fingerprint',marker_fp),
        'SNAPSHOT_KEY_MATCHES_MARKER': hmac.compare_digest(primary[0].upper(),marker_fp),
        'INVITE_NUMBER_PRESENT': invite_present,
        'AUTH_TOKEN_PRESENT': auth_present,
        'AUTH_TOKEN_FRESH': matched_auth_fresh,
    }
    for name, value in results.items():
        print('SAFE_'+name+'='+('YES' if value else 'NO'))
    print('SAFE_SNAPSHOT_CREATED=' + str(asset.get('created_at','unknown')))
    db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('SAFE_INVITE_AUDIT_ERROR=' + type(exc).__name__)
        sys.exit(1)
