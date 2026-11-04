#!/usr/bin/env python3
"""Authenticate recovery material in memory; never log private metadata.

This read-only diagnostic never restores an account, writes decrypted files,
queries conversations, or prints passwords, addresses, fingerprints or IDs.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import PurePosixPath
import re
import sqlite3
import sys
import tarfile
import tomllib

import requests

# Executed as a script from tools/, so make the repository root importable.
sys.path.insert(0, str(PurePosixPath(__file__).parent.parent))
import state_backup  # noqa: E402


def memory_integrity(plain):
    """Only structure / SQLite quick_check, without restoring messages."""
    with tarfile.open(fileobj=io.BytesIO(plain), mode='r:*') as archive:
        members = archive.getmembers(); names = set(); databases = set(); required = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (not path.parts or path.is_absolute() or '..' in path.parts or member.name in names
                    or not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError('unsafe container')
            names.add(member.name)
        manifests = [x for x in members if x.isfile() and x.name.endswith('/accounts/accounts.toml')]
        if len(manifests) != 1 or manifests[0].size > 1_000_000:
            raise ValueError('manifest missing')
        manifest = tomllib.loads(archive.extractfile(manifests[0]).read().decode('utf-8'))
        accounts = manifest.get('accounts')
        if not isinstance(accounts, list) or not accounts:
            raise ValueError('account missing')
        prefix = manifests[0].name.removesuffix('accounts.toml')
        for item in accounts:
            folder = item.get('dir') if isinstance(item, dict) else None
            if not isinstance(folder, str) or not folder or PurePosixPath(folder).name != folder:
                raise ValueError('invalid account')
            required.add(prefix + folder + '/dc.db')
        for member in members:
            if not member.isfile() or member.size < 16:
                continue
            stream = archive.extractfile(member); head = stream.read(16)
            if head != b'SQLite format 3\0':
                continue
            if member.size > 384_000_000:
                raise ValueError('database too large')
            data = head + stream.read()
            if len(data) != member.size:
                raise ValueError('incomplete database')
            with sqlite3.connect(':memory:') as db:
                db.deserialize(data)
                if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                    raise ValueError('database integrity')
            databases.add(member.name)
        if not required.issubset(databases):
            raise ValueError('database missing')


def strings(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from strings(value)
    elif isinstance(obj, str) and 16 <= len(obj) <= 512:
        yield obj


def main():
    repo = os.environ.get('STATE_BACKUP_REPO', '')
    token = os.environ.get('STATE_GITHUB_TOKEN', '')
    passwords = [os.environ.get('CLEAN_PASSWORD', ''), os.environ.get('OLD_PASSWORD', '')]
    passwords = [x for x in passwords if x]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not token or not passwords:
        raise ValueError('audit configuration')
    api='https://api.github.com/repos/'+repo
    session=requests.Session(); session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                                                         'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Michiflin-readonly-audit'})
    response=session.get(api,timeout=35);response.raise_for_status()
    if response.json().get('private') is not True:raise ValueError('repository not private')
    response=session.get(api+'/releases/tags/'+state_backup.RELEASE_TAG,timeout=35);response.raise_for_status();release=response.json()
    if not str(release.get('body') or '').startswith(state_backup.RELEASE_MARKER):raise ValueError('untrusted release')
    items=[]
    for page in range(1,12):
        response=session.get(api+'/releases/'+str(release['id'])+'/assets',params={'per_page':100,'page':page},timeout=35)
        response.raise_for_status();batch=response.json();items.extend(batch)
        if len(batch)<100:break
    else:raise ValueError('incomplete listing')
    def read(item):
        size=item.get('size');digest=item.get('digest')
        if (type(size) is not int or not 0<size<90_000_000 or not isinstance(digest,str)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest)):
            raise ValueError('asset metadata')
        response=session.get(api+'/releases/assets/'+str(item['id']),headers={'Accept':'application/octet-stream'},timeout=120)
        response.raise_for_status();blob=response.content
        if len(blob)!=size or hashlib.sha256(blob).hexdigest()!=digest[7:]:raise ValueError('asset hash')
        return blob
    plans=[x for x in items if re.fullmatch(r'michiflin-owner-clean-plan-v1-[0-9a-f]{24}\.bin',x.get('name',''))]
    pairs=[x for x in items if re.fullmatch(r'michiflin-owner-clean-candidate-v1-[0-9a-f]{24}-paired-\d{8}T\d{6}Z-[0-9a-f]{8}\.bin',x.get('name',''))]
    if len(plans)!=1 or len(pairs)!=2:raise ValueError('candidate set changed')
    plain_plan=None
    blob=read(plans[0])
    for password in list(passwords):
        try:plain_plan=state_backup.decrypt_backup(blob,password);break
        except ValueError:pass
    if plain_plan is not None:
        try:
            metadata=json.loads(plain_plan)
            # Candidate passwords may have been sealed inside the recovery plan.
            for value in strings(metadata):
                if value not in passwords and len(passwords)<50:passwords.append(value)
        except (ValueError,UnicodeError):pass
    paired_auth=paired_verified=0
    for item in pairs:
        blob=read(item);plain=None
        for password in passwords:
            try:plain=state_backup.decrypt_backup(blob,password);break
            except ValueError:continue
        if plain is None:continue
        paired_auth+=1
        try:memory_integrity(plain);paired_verified+=1
        except Exception:pass
    print('SAFE_CANDIDATE_AUDIT',json.dumps({'plan_authenticated':plain_plan is not None,
                                            'paired_authenticated':paired_auth,
                                            'paired_memory_verified':paired_verified},sort_keys=True))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('SAFE_CANDIDATE_AUDIT_UNAVAILABLE',type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
