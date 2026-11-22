"""Opt-in, additional encrypted Google Drive snapshots. Never a primary restore source.

Use an owner's Google OAuth refresh grant and an unshared My Drive folder.
No credentials in bot state, WebXDC, logs, Drive metadata or uploaded payloads.
Disabled unless DRIVE_BACKUP_ENABLED=1 and all four GOOGLE_DRIVE_* secrets exist.
No Drive or primary backup is deleted by this module.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets

import requests

API = 'https://www.googleapis.com/drive/v3/files'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
KEYS = ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET', 'GOOGLE_DRIVE_REFRESH_TOKEN', 'GOOGLE_DRIVE_FOLDER_ID')


def enabled() -> bool:
    return os.getenv('DRIVE_BACKUP_ENABLED', '0').strip() == '1'


def configured() -> bool:
    return enabled() and all(os.getenv(k, '').strip() for k in KEYS)


def _id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', value):
        raise ValueError('invalid Drive id')
    return value


def _headers() -> dict[str, str]:
    if not configured():
        raise ValueError('Drive backup credentials are incomplete')
    response = requests.post('https://oauth2.googleapis.com/token', data={
        'grant_type': 'refresh_token', 'client_id': os.environ[KEYS[0]],
        'client_secret': os.environ[KEYS[1]], 'refresh_token': os.environ[KEYS[2]],
    }, timeout=30, allow_redirects=False)
    response.raise_for_status()
    token = response.json().get('access_token')
    if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
        raise ValueError('Drive token missing')
    return {'Authorization': 'Bearer ' + token}


def _download(file_id: str, headers: dict[str, str], limit: int) -> bytes:
    with requests.get(API + '/' + _id(file_id), params={'alt': 'media'}, headers=headers,
                      timeout=(30, 180), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('Drive download refused')
        data = bytearray()
        for block in response.iter_content(1024 * 1024):
            if len(data) + len(block) > limit:
                raise ValueError('Drive snapshot exceeds limit')
            data.extend(block)
        return bytes(data)


def mirror(encrypted: bytes, name: str, passphrase: str) -> dict:
    """Upload only the verified encrypted envelope; read back and test recovery.

    An incomplete upload stays named pending. No old files are replaced/deleted,
    even if upload, verification, quota, authorization or renaming fails.
    """
    import state_backup as backup
    if not enabled():
        return {'status': 'disabled'}
    if not configured():
        raise ValueError('Drive backup credentials are incomplete')
    if not encrypted.startswith(backup.MAGIC) or len(encrypted) > backup.MAX_RELEASE_BACKUP_BYTES:
        raise ValueError('not a supported encrypted backup')
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.bin', name):
        raise ValueError('invalid backup name')
    backup._verify_plaintext(backup.decrypt_backup(encrypted, passphrase))
    folder_id = _id(os.environ[KEYS[3]])
    headers = _headers()
    folder = requests.get(API + '/' + folder_id, headers=headers,
                          params={'fields': 'id,mimeType,trashed,shared'}, timeout=30, allow_redirects=False)
    folder.raise_for_status()
    metadata = folder.json()
    if (metadata.get('mimeType') != 'application/vnd.google-apps.folder' or metadata.get('trashed')
            or metadata.get('shared') is not False):
        raise ValueError('Drive folder must be an unshared private folder')
    digest = hashlib.sha256(encrypted).hexdigest()
    # Multipart body uses bytes, not text/base64. No plaintext is ever uploaded.
    boundary = 'michiflin_' + secrets.token_hex(16)
    import json
    metadata = {'name': 'pending-' + name, 'parents': [folder_id], 'mimeType': 'application/octet-stream',
                'appProperties': {'michiflin': 'encrypted-v1', 'verified': 'false', 'sha256': digest}}
    payload = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()
               + json.dumps(metadata).encode() + f'\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
               + encrypted + f'\r\n--{boundary}--\r\n'.encode())
    response = requests.post(UPLOAD, params={'uploadType': 'multipart', 'fields': 'id,name'},
                             headers={**headers, 'Content-Type': 'multipart/related; boundary=' + boundary},
                             data=payload, timeout=(30, 180), allow_redirects=False)
    response.raise_for_status()
    file_id = _id(response.json().get('id'))
    downloaded = _download(file_id, headers, len(encrypted))
    if not secrets.compare_digest(hashlib.sha256(downloaded).hexdigest(), digest):
        raise ValueError('Drive readback mismatch; primary backups preserved')
    backup._verify_plaintext(backup.decrypt_backup(downloaded, passphrase))
    response = requests.patch(API + '/' + file_id, headers=headers, json={
        'name': name, 'appProperties': {'michiflin': 'encrypted-v1', 'verified': 'true', 'sha256': digest}},
        params={'fields': 'id,name,appProperties'}, timeout=30, allow_redirects=False)
    response.raise_for_status()
    value = response.json()
    if value.get('id') != file_id or value.get('name') != name or value.get('appProperties', {}).get('verified') != 'true':
        raise ValueError('Drive verification marker missing')
    return {'status': 'verified', 'id': file_id, 'sha256': digest}


def recovery_archive(file_id: str, passphrase: str) -> bytes:
    """Explicit recovery into an archive for inspection; never overwrite live state.

    Operator must first configure credentials. The returned archive can contain
    identity secrets: handle offline, validate the identity, and do not log it.
    """
    import state_backup as backup
    downloaded = _download(_id(file_id), _headers(), backup.MAX_RELEASE_BACKUP_BYTES)
    plaintext = backup.decrypt_backup(downloaded, passphrase)
    backup._verify_plaintext(plaintext)
    return plaintext
