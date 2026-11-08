"""Private, one-time transfer of current contact invitation tokens.

Only the clean identity marker, configured address, and two contact-token
values are read. Everything remains in RAM and is encrypted end-to-end before
upload to an existing *private* release. No Delta account is started, no
message or chat is queried, and no key, invitation or address is logged.
"""
from __future__ import annotations
import base64, hashlib, io, json, os, re, sqlite3, tarfile
from datetime import datetime, timezone
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAGIC=b'MICHIFLIN-STATE-1\0'
INFO=b'michiflin-live-private-invite-transfer-v1'

def main() -> None:
    token=os.environ['GITHUB_TOKEN']; password=os.environ['STATE_BACKUP_PASSPHRASE']
    repo=os.environ['STATE_BACKUP_REPO']; expected=os.environ['EXPECTED_IDENTITY_DIGEST']
    transfer_id=os.environ['TRANSFER_ID']; pub_raw=base64.b64decode(os.environ['TRANSFER_PUBLIC_KEY'],validate=True)
    if repo!='YosvanyAlain/michiflin-private-state' or not re.fullmatch(r'[0-9a-f]{24}',transfer_id) or not re.fullmatch(r'[0-9a-f]{64}',expected) or len(pub_raw)!=32:
        raise ValueError('input_validation')
    h={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    base='https://api.github.com/repos/'+repo
    resp=requests.get(base,headers=h,timeout=30);resp.raise_for_status()
    if resp.json().get('private') is not True:
        raise ValueError('not_private')
    resp=requests.get(base+'/releases/tags/michiflin-state-releases-v1',headers=h,timeout=30);resp.raise_for_status();release=resp.json()
    assets=[a for a in release.get('assets',[]) if re.fullmatch(r'michiflin-owner-clean-state-\d{8}T\d{6}Z-[0-9a-f]{16}\.bin',str(a.get('name','')))]
    if not assets: raise ValueError('missing_backup')
    latest=max(assets,key=lambda x:x['name'])
    created=datetime.fromisoformat(latest['created_at'].replace('Z','+00:00'))
    if not 0 <= (datetime.now(timezone.utc)-created).total_seconds() < 12*3600:
        raise ValueError('backup_stale')
    resp=requests.get(base+'/releases/assets/'+str(int(latest['id'])),headers={**h,'Accept':'application/octet-stream'},timeout=120);resp.raise_for_status();encrypted=resp.content
    if not encrypted.startswith(MAGIC) or not 0<len(encrypted)<=90_000_000:raise ValueError('invalid_backup')
    salt=encrypted[len(MAGIC):len(MAGIC)+16]
    derived=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,480_000,dklen=32)
    archive=Fernet(base64.urlsafe_b64encode(derived)).decrypt(encrypted[len(MAGIC)+16:])
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tar:
        dbs=[m for m in tar.getmembers() if m.isfile() and m.name.endswith('/dc.db')]
        if len(dbs)!=1 or dbs[0].size>50_000_000:raise ValueError('invalid_database')
        marker=tar.getmember('michiflin/.owner-clean-origin-v1.json')
        if not marker.isfile() or marker.size>4096:raise ValueError('invalid_marker')
        marker_data=json.loads(tar.extractfile(marker).read())
        db_data=tar.extractfile(dbs[0]).read()
    if marker_data.get('confirmed') is not True or marker_data.get('fresh_account') is not True:
        raise ValueError('unconfirmed_identity')
    fingerprint=''.join(c for c in str(marker_data.get('bot_fingerprint','')).upper() if c in '0123456789ABCDEF')
    with sqlite3.connect(':memory:') as db:
        db.deserialize(db_data)
        row=db.execute("SELECT value FROM config WHERE keyname='configured_addr' LIMIT 1").fetchone()
        inv=db.execute("SELECT token FROM tokens WHERE namespc=100 AND COALESCE(foreign_key,'')='' ORDER BY id DESC LIMIT 1").fetchone()
        auth=db.execute("SELECT token FROM tokens WHERE namespc=110 AND COALESCE(foreign_key,'')='' ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not inv or not auth or not all(isinstance(v,str) and 8<=len(v)<=512 for v in (row[0],inv[0],auth[0])):
        raise ValueError('missing_invitation_data')
    address=row[0].strip().casefold()
    if len(fingerprint)!=40 or hashlib.sha256((address+'\0'+fingerprint).encode()).hexdigest()!=expected:
        raise ValueError('identity_mismatch')
    payload=json.dumps({'version':1,'address':address,'fingerprint':fingerprint,'i':inv[0],'s':auth[0],
                        'snapshot_id':int(latest['id']),'snapshot_created':latest['created_at']},separators=(',',':')).encode()
    ephemeral=X25519PrivateKey.generate();peer=X25519PublicKey.from_public_bytes(pub_raw)
    key=HKDF(algorithm=hashes.SHA256(),length=32,salt=None,info=INFO).derive(ephemeral.exchange(peer))
    nonce=os.urandom(12);ciphertext=AESGCM(key).encrypt(nonce,payload,transfer_id.encode())
    encode=lambda b:base64.b64encode(b).decode()
    envelope=json.dumps({'version':1,'ephemeral_public_key':encode(ephemeral.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)),
                         'nonce':encode(nonce),'ciphertext':encode(ciphertext)},separators=(',',':')).encode()
    name='michiflin-live-invite-transfer-'+transfer_id+'.enc'
    if any(a.get('name')==name for a in release.get('assets',[])):raise ValueError('duplicate_transfer')
    upload='https://uploads.github.com/repos/'+repo+'/releases/'+str(int(release['id']))+'/assets'
    resp=requests.post(upload,params={'name':name},headers={**h,'Content-Type':'application/octet-stream'},data=envelope,timeout=30);resp.raise_for_status()
    if resp.json().get('name')!=name:raise ValueError('upload_mismatch')
    print('SAFE_PRIVATE_ENCRYPTED_TRANSFER_UPLOADED')
    print('SAFE_ONLY_INVITATION_TOKEN_AND_IDENTITY_METADATA_QUERIED')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        allowed={'input_validation','not_private','missing_backup','backup_stale','invalid_backup','invalid_database','invalid_marker','unconfirmed_identity','missing_invitation_data','identity_mismatch','duplicate_transfer','upload_mismatch'}
        print('SAFE_PRIVATE_TRANSFER_FAILED_'+type(exc).__name__)
        if isinstance(exc, ValueError) and str(exc) in allowed: print('SAFE_REASON_'+str(exc))
        raise SystemExit(1) from None
