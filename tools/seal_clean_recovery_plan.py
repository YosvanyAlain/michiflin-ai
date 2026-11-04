#!/usr/bin/env python3
"""Seal non-secret recovery instructions after memory-only backup checks.

No old account or message is restored, no credential is written to the plan,
and no private metadata is printed. The original recovery plan is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import state_backup  # noqa: E402
from tools.audit_candidate_memory_only import memory_integrity  # noqa: E402


def main():
    repo=os.environ.get('STATE_BACKUP_REPO','');token=os.environ.get('STATE_GITHUB_TOKEN','')
    password=os.environ.get('CLEAN_PASSWORD','');epoch=os.environ.get('CLEAN_EPOCH','')
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not token or not password
            or not re.fullmatch(r'[0-9a-f]{48}',epoch)):
        raise ValueError('invalid recovery configuration')
    os.environ['MICHIFLIN_CLEAN_EPOCH']=epoch
    api='https://api.github.com/repos/'+repo
    session=requests.Session();session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                                                       'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Michiflin-recovery-plan'})
    response=session.get(api,timeout=35);response.raise_for_status()
    if response.json().get('private') is not True:raise ValueError('backup repository not private')
    response=session.get(api+'/releases/tags/'+state_backup.RELEASE_TAG,timeout=35);response.raise_for_status();release=response.json()
    if (not str(release.get('body') or '').startswith(state_backup.RELEASE_MARKER)
            or type(release.get('id')) is not int):raise ValueError('untrusted release')
    items=[]
    for page in range(1,12):
        response=session.get(api+'/releases/'+str(release['id'])+'/assets',params={'per_page':100,'page':page},timeout=35)
        response.raise_for_status();batch=response.json()
        if not isinstance(batch,list):raise ValueError('invalid asset listing')
        items.extend(batch)
        if len(batch)<100:break
    else:raise ValueError('asset listing incomplete')
    def read(item):
        size=item.get('size');digest=item.get('digest')
        if (type(item.get('id')) is not int or type(size) is not int or not 0<size<90_000_000
                or not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest)):
            raise ValueError('invalid asset metadata')
        response=session.get(api+'/releases/assets/'+str(item['id']),headers={'Accept':'application/octet-stream'},timeout=180)
        response.raise_for_status();blob=response.content
        if len(blob)!=size or hashlib.sha256(blob).hexdigest()!=digest[7:]:raise ValueError('encrypted asset mismatch')
        return blob
    original_plans=[x for x in items if x.get('state')=='uploaded' and isinstance(x.get('name'),str)
                    and re.fullmatch(r'michiflin-owner-clean-plan-v1-[0-9a-f]{24}\.bin',x['name'])]
    if len(original_plans)!=1:raise ValueError('original candidate plan not present')
    pattern=state_backup.CLEAN_OWNER_ASSET_PATTERN
    snapshots=sorted((x for x in items if x.get('state')=='uploaded' and isinstance(x.get('name'),str)
                      and pattern.fullmatch(x['name'])),key=lambda x:(x.get('created_at',''),x['id']),reverse=True)
    if len(snapshots)<2:raise ValueError('fewer than two clean snapshots')
    # Preserve and verify the oldest clean seed, plus two independently readable
    # newest snapshots, while never extracting their files from the container.
    seed=snapshots[-1]
    checked=[]
    for item in [snapshots[0],snapshots[1],seed]:
        if item['id'] in checked:continue
        plain=state_backup.decrypt_backup(read(item),password)
        state_backup._verify_clean_origin(plain)
        memory_integrity(plain)
        checked.append(item['id'])
    if len(checked)<2:raise ValueError('insufficient independent backups')
    name='michiflin-owner-clean-recovery-plan-v2-'+epoch[:24]+'.bin'
    existing=[x for x in items if x.get('name')==name and x.get('state')=='uploaded']
    if len(existing)>1:raise ValueError('duplicate recovery plan')
    required={
      'version':2,'kind':'clean-owner-recovery-instructions','epoch':epoch,
      'repository':repo,'release_tag':state_backup.RELEASE_TAG,
      'namespace':state_backup.CLEAN_OWNER_NAMESPACE,'seed_asset_id':seed['id'],
      'backup_passphrase_secret':'CLEAN_BACKUP_PASSPHRASE',
      'backup_token_secret':'STATE_GITHUB_TOKEN',
      'backup_repository_secret':'STATE_BACKUP_REPO',
      'epoch_variable':'CLEAN_IDENTITY_EPOCH',
      'old_candidate_key_included':False,
      'instructions':[
        'Recovery is an explicit operation only after authorization; do not run during normal maintenance.',
        'Stop and fence every bot instance before a recovery; never run the same identity on two instances.',
        'Use the named private repository and release with the owner-clean-v1 namespace and the exact epoch.',
        'With the existing protected Actions secrets, restore only into a new empty private location using state_backup.py restore.',
        'The normal clean restore rejects an invalid newest snapshot rather than silently replaying an older one.',
        'Run tools/clean_owner_identity.py activate and independently verify the authorized owner before service starts.',
        'Never import legacy accounts, old contacts or historical messages as part of this plan.',
        'The seed is retained as an anchor; newer snapshot IDs may rotate, so select by the clean namespace.',
        'The pre-existing encrypted candidate plan is separate and is not superseded by these instructions.'
      ]}
    if existing:
        item=existing[0];plain=state_backup.decrypt_backup(read(item),password);value=json.loads(plain)
        if any(value.get(k)!=v for k,v in required.items()):raise ValueError('existing plan does not match')
    else:
        payload={**required,'created_utc':datetime.now(timezone.utc).isoformat(),
                 'independent_snapshots_verified_at_creation':len(checked)}
        plain=json.dumps(payload,sort_keys=True,separators=(',',':')).encode('utf-8')
        salt=os.urandom(state_backup.SALT_BYTES)
        blob=state_backup.MAGIC+salt+state_backup.fernet_from(password,salt).encrypt(plain)
        upload='https://uploads.github.com/repos/'+repo+'/releases/'+str(release['id'])+'/assets'
        response=session.post(upload,params={'name':name},headers={'Content-Type':'application/octet-stream'},
                              data=blob,timeout=120,allow_redirects=False)
        response.raise_for_status();item=response.json()
        if item.get('name')!=name or item.get('state')!='uploaded':raise ValueError('plan upload not finalized')
        # Confirm the provider recorded the digest before comparing its download.
        response=session.get(api+'/releases/assets/'+str(item['id']),timeout=35);response.raise_for_status();item=response.json()
        reread=read(item)
        if hashlib.sha256(blob).digest()!=hashlib.sha256(reread).digest():raise ValueError('plan download mismatch')
        if json.loads(state_backup.decrypt_backup(reread,password))!=payload:raise ValueError('plan verification failed')
    # Recheck that the original plan and the independently verified copies
    # still exist after the upload; a concurrent rotation must fail closed.
    for asset_id in checked+[original_plans[0]['id']]:
        response=session.get(api+'/releases/assets/'+str(asset_id),timeout=35)
        response.raise_for_status()
    print('SAFE_CLEAN_RECOVERY_PLAN_VERIFIED',json.dumps({'snapshots_verified':len(checked),
          'original_plan_preserved':True,
          'new_plan_recoverable_with_existing_secret':True},sort_keys=True))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('SAFE_CLEAN_RECOVERY_PLAN_FAILED',type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
