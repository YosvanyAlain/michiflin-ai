"""Compare an invitation digest with current clean-state token metadata.

No bot process or network identity is started. The authenticated backup and
its database remain in memory. No contacts, message tables, blobs, addresses,
keys, tokens or invitation URLs are read into logs or artifacts.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import io
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
    # Memory-only SQLite: only the SecureJoin token namespace is queried.
    db = sqlite3.connect(':memory:')
    db.deserialize(database_bytes)
    db.execute('PRAGMA query_only = ON')
    rows = db.execute("SELECT namespc, COUNT(*), MAX(timestamp) FROM tokens WHERE namespc IN (100,110) AND (foreign_key='' OR foreign_key IS NULL) GROUP BY namespc").fetchall()
    counts = {int(namespace): int(count) for namespace, count, _ in rows}
    latest = {int(namespace): int(timestamp or 0) for namespace, _, timestamp in rows}
    print('SAFE_CONTACT_INVITE_COUNT=' + str(counts.get(100, 0)))
    print('SAFE_CONTACT_AUTH_COUNT=' + str(counts.get(110, 0)))
    if latest.get(110):
        age = int(time.time()) - latest[110]
        print('SAFE_NEWEST_AUTH_AGE_HOURS=' + str(max(0, age // 3600)))
        print('SAFE_NEWEST_AUTH_FRESH=' + ('YES' if 0 <= age < 7 * 86400 else 'NO'))
    print('SAFE_SNAPSHOT_CREATED=' + str(asset.get('created_at', 'unknown')))
    db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('SAFE_INVITE_AUDIT_ERROR=' + type(exc).__name__)
        sys.exit(1)
