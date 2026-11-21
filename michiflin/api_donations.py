"""Explicit, revocable API-key donations: encrypted release assets in a private repo.

A donation is pending review, not silently activated as a shared provider. The
existing personal key remains personal unless this separate consent is given.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import threading
from urllib.parse import quote

import requests
from . import personal_ai

CONSENT_VERSION = 'api-donation-v1'
RELEASE_TAG = 'michiflin-api-donations-v1'
MAX_DONATIONS = 8
_lock = threading.RLock()


def _configuration() -> tuple[str, str]:
    repo = os.getenv('DONATION_REPO', os.getenv('STATE_BACKUP_REPO', '')).strip()
    token = os.getenv('GITHUB_TOKEN', '').strip()
    if not token or not repo or repo.count('/') != 1:
        raise personal_ai.PersonalError('donation_unavailable')
    return repo, token


def _request(method: str, url: str, token: str, **kwargs):
    try:
        response = requests.request(method, url, headers={'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json', **kwargs.pop('headers', {})},
            allow_redirects=False, timeout=35, **kwargs)
        if response.status_code not in {200, 201, 204, 404, 422}:
            raise personal_ai.PersonalError('donation_storage')
        return response
    except requests.RequestException:
        raise personal_ai.PersonalError('donation_storage') from None


def _base(repo: str) -> str:
    return 'https://api.github.com/repos/' + quote(repo, safe='/')


def _records(address: str) -> dict:
    data = personal_ai._load(address)
    records = data.get('donations', {})
    return records if isinstance(records, dict) else {}


def _store_record(address: str, provider_id: str, record: dict) -> None:
    with personal_ai._lock:
        current = personal_ai._load(address)
        if record.get('state') != 'revoked':
            personal_ai._find(current, provider_id)
        current.setdefault('donations', {})[provider_id] = dict(record)
        personal_ai._save(address, current)


def _verify_remote(repo: str, token: str, asset_id: int, address: str, provider: dict, record: dict) -> str:
    try:
        response = requests.get(_base(repo) + '/releases/assets/' + str(asset_id),
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/octet-stream'},
            stream=True, timeout=35)
        try:
            response.raise_for_status()
            blob = bytearray()
            for chunk in response.iter_content(8192):
                if len(blob) + len(chunk) > 64_000:
                    raise ValueError('limit')
                blob.extend(chunk)
        finally:
            response.close()
        value = json.loads(personal_ai._fernet(b'api-donation').decrypt(bytes(blob)))
        if (value.get('donor') != address or value.get('provider') != provider
                or value.get('consent', {}).get('version') != CONSENT_VERSION
                or value.get('consent', {}).get('daily') != record['daily']):
            raise ValueError('binding')
        return hashlib.sha256(blob).hexdigest()
    except Exception:
        raise personal_ai.PersonalError('donation_verification') from None


def status(address: str) -> list[dict]:
    records = _records(address)
    return [{'provider': key, 'state': value.get('state'), 'daily': value.get('daily'),
             'consent_version': value.get('consent_version'), 'created': value.get('created')}
            for key, value in records.items() if isinstance(value, dict)]


def donate(address: str, provider_id: str, *, consent: object, version: object, daily: object) -> dict:
    if consent is not True or version != CONSENT_VERSION or type(daily) is not int or not 1 <= daily <= 1000:
        raise personal_ai.PersonalError('consent_required')
    repo, token = _configuration()
    base = _base(repo)
    response = _request('GET', base, token)
    metadata = response.json()
    if response.status_code != 200 or metadata.get('private') is not True or metadata.get('archived') is True:
        raise personal_ai.PersonalError('donation_private_repo')
    with _lock:
        data = personal_ai._load(address)
        provider = personal_ai._find(data, provider_id)
        records = data.setdefault('donations', {})
        record = records.get(provider_id)
        if record and record.get('state') == 'pending_review':
            return {'donations': status(address), 'accepted': True, 'active': False}
        if not record and len(records) >= MAX_DONATIONS:
            raise personal_ai.PersonalError('donation_limit')
        response = _request('GET', base + '/releases/tags/' + RELEASE_TAG, token)
        if response.status_code == 404:
            response = _request('POST', base + '/releases', token, json={'tag_name': RELEASE_TAG,
                'name': 'Encrypted voluntary API donations', 'body': 'Owner-reviewed encrypted contributions. No credentials in source.', 'draft': False, 'prerelease': True})
            if response.status_code == 422:
                response = _request('GET', base + '/releases/tags/' + RELEASE_TAG, token)
        if response.status_code not in {200, 201}:
            raise personal_ai.PersonalError('donation_storage')
        release = response.json()
        release_id = release.get('id')
        if type(release_id) is not int or release_id < 1:
            raise personal_ai.PersonalError('donation_storage')
        if not record or record.get('state') == 'revoked':
            record = {'state': 'uploading', 'name': 'api-donation-' + secrets.token_hex(16) + '.enc',
                      'created': time.time(), 'daily': daily, 'consent_version': CONSENT_VERSION,
                      'repository': repo, 'release_id': release_id}
            records[provider_id] = record
            _store_record(address, provider_id, record)  # stable intent before remote handoff
        shared_provider = {key: provider[key] for key in ['id', 'name', 'kind', 'base', 'key'] if key in provider}
        payload = {'version': 1, 'donor': address, 'provider': shared_provider, 'consent': {
            'version': CONSENT_VERSION, 'scope': 'shared-bot-pending-owner-review', 'daily': record['daily'], 'at': record['created']}}
        encrypted = personal_ai._fernet(b'api-donation').encrypt(json.dumps(payload, separators=(',', ':')).encode())
        # Retry reconciliation by the persisted random name, never duplicate a lost response.
        response = _request('GET', base + f'/releases/{release_id}/assets?per_page=100', token)
        assets = response.json()
        if response.status_code != 200 or not isinstance(assets, list):
            raise personal_ai.PersonalError('donation_storage')
        existing = next((asset for asset in assets if asset.get('name') == record['name']), None)
        if existing is None:
            response = _request('POST', 'https://uploads.github.com/repos/' + quote(repo, safe='/') + f'/releases/{release_id}/assets',
                                token, params={'name': record['name']}, data=encrypted, headers={'Content-Type': 'application/octet-stream'})
            if response.status_code != 201:
                raise personal_ai.PersonalError('donation_storage')
            existing = response.json()
        if type(existing.get('id')) is not int:
            raise personal_ai.PersonalError('donation_storage')
        record['asset_id'] = existing['id']
        _store_record(address, provider_id, record)
        digest = _verify_remote(repo, token, existing['id'], address, shared_provider, record)
        record.update(state='pending_review', payload_sha256=digest)
        _store_record(address, provider_id, record)
        return {'donations': status(address), 'accepted': True, 'active': False}


def revoke(address: str, provider_id: str) -> dict:
    repo, token = _configuration()
    with _lock:
        data = personal_ai._load(address)
        record = data.get('donations', {}).get(provider_id)
        if not record or record.get('state') == 'revoked':
            return {'donations': status(address)}
        if record.get('repository') != repo:
            raise personal_ai.PersonalError('donation_storage')
        if type(record.get('asset_id')) is not int:
            response = _request('GET', _base(repo) + f"/releases/{record['release_id']}/assets?per_page=100", token)
            if response.status_code != 200 or not isinstance(response.json(), list):
                raise personal_ai.PersonalError('donation_storage')
            asset = next((item for item in response.json() if item.get('name') == record.get('name')), None)
            if asset is None:
                record.update(state='revoked', revoked=time.time())
                _store_record(address, provider_id, record)
                return {'donations': status(address)}
            record['asset_id'] = asset['id']
        response = _request('DELETE', _base(repo) + '/releases/assets/' + str(record['asset_id']), token)
        if response.status_code not in {204, 404}:
            raise personal_ai.PersonalError('donation_storage')
        record.update(state='revoked', revoked=time.time())
        _store_record(address, provider_id, record)
    return {'donations': status(address)}
