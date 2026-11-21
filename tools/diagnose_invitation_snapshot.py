"""Inspect an isolated backup, no mail I/O and no private fields exported."""
import hashlib,json,os,sqlite3,subprocess,tempfile
from pathlib import Path
EXPECTED={'address': '4a192f25d0f1b4c64124ce00b43b8d63b5f141b00deaa72226f3eac726c11ebc', 'invite': '3614434a2355980e907e35e184e44eeeb84505403555a42abc587bfd25dc337f', 'auth': '9e771c0cd4e0c0ffddec8724b158c99579d8de1c0a3d03d80b0df1efffb29bda'}
FINGERPRINT='45824F71C9950BC6C2B1E8ABA2B91F8FF7624438'
def digest(v):
 return hashlib.sha256(str(v).strip().lower().encode()).hexdigest()
def token_digest(v):
 return hashlib.sha256(str(v).encode()).hexdigest()
r={'read_only':True,'mail_io_started':False,'address_matches':False,'fingerprint_matches':False,'invitation_token_present':False,'auth_token_present':False,'database_ok':False}
files=list((Path(os.environ['CONFIG_DIR'])/'accounts').rglob('dc.db'));r['database_count']=len(files)
if len(files)==1:
 with sqlite3.connect('file:'+str(files[0])+'?mode=ro',uri=True) as db:
  r['database_ok']=db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
  tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  for name,value in db.execute('SELECT keyname,value FROM config'):
   if name in {'addr','configured_addr'} and digest(value)==EXPECTED['address']:r['address_matches']=True
  if 'tokens' in tables:
   for row in db.execute('SELECT * FROM tokens'):
    for value in row:
     if isinstance(value,str):
      d=token_digest(value)
      r['invitation_token_present']|=d==EXPECTED['invite'];r['auth_token_present']|=d==EXPECTED['auth']
  if 'keypairs' in tables:
   cols=[x[1] for x in db.execute('PRAGMA table_info(keypairs)')]
   r['keypair_count']=db.execute('SELECT count(*) FROM keypairs').fetchone()[0]
   for col in ['public_key','public_key_data']:
    if col not in cols:continue
    for (key,) in db.execute('SELECT '+col+' FROM keypairs'):
     with tempfile.TemporaryDirectory() as home:
      x=subprocess.run(['gpg','--no-options','--batch','--no-autostart','--homedir',home,'--with-colons','--show-keys'],input=key,capture_output=True,timeout=8)
      for line in x.stdout.decode(errors='replace').splitlines():
       f=line.split(':')
       if f[0]=='fpr' and len(f)>9:r['fingerprint_matches']|=f[9].upper()==FINGERPRINT
assert all(isinstance(x,(bool,int)) for x in r.values())
print(json.dumps(r,sort_keys=True))
