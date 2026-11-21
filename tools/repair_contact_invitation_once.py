"""Restore only an explicitly authorized contact-link token before mail I/O.

No identity, key, grant, miniapp binding or contact is created or replaced.
The deployment must be stopped/restored and armed for one exact revision.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def parse_link(link: str) -> tuple[str, str, str, str]:
    u = urlsplit(link)
    if u.scheme != 'https' or u.netloc != 'i.delta.chat' or u.path != '/' or u.query:
        raise ValueError('unsupported invitation')
    fingerprint, sep, rest = u.fragment.partition('&')
    q = parse_qs(rest, strict_parsing=True)
    if (not sep or not re.fullmatch('[A-Fa-f0-9]{40}', fingerprint)
            or set(q) - {'v', 'i', 's', 'a', 'n'} or any(len(v) != 1 for v in q.values())
            or q.get('v') != ['3'] or not all(k in q for k in ['i', 's', 'a'])):
        raise ValueError('unsupported invitation')
    invite, auth, address = q['i'][0], q['s'][0], q['a'][0].strip().casefold()
    if (not all(re.fullmatch(r'[A-Za-z0-9_-]{20,128}', x) for x in [invite, auth])
            or not re.fullmatch(r'[^\s@]{1,128}@[^\s@]{1,255}', address)):
        raise ValueError('malformed invitation')
    return fingerprint.upper(), address, invite, auth


def repair(root: Path, link: str, checkpoint, fingerprint_from_key) -> dict:
    fingerprint, address, invite, auth = parse_link(link)
    files = list((root / 'accounts').rglob('dc.db'))
    if len(files) != 1:
        raise ValueError('one existing account is required')
    with sqlite3.connect(files[0]) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('account integrity failed')
        addresses = [str(v).casefold() for (v,) in db.execute(
            "SELECT value FROM config WHERE keyname IN ('addr','configured_addr')")]
        keys = db.execute('SELECT public_key FROM keypairs').fetchall()
        if address not in addresses or len(keys) != 1 or fingerprint_from_key(keys[0][0]) != fingerprint:
            raise ValueError('invitation identity mismatch')
        if not db.execute("SELECT 1 FROM tokens WHERE namespc=100 AND foreign_key='' AND token=?", (invite,)).fetchone():
            raise ValueError('contact invitation is not already registered')
        exists = bool(db.execute("SELECT 1 FROM tokens WHERE namespc=110 AND foreign_key='' AND token=?", (auth,)).fetchone())
        if not exists:
            db.execute("INSERT INTO tokens(namespc,foreign_key,token,timestamp) VALUES(110,'',?,?)", (auth, int(time.time())))
            db.commit()
    try:
        checkpoint()  # required encrypted, verified snapshot before serving
    except Exception:
        if not exists:
            with sqlite3.connect(files[0]) as db:
                db.execute("DELETE FROM tokens WHERE namespc=110 AND foreign_key='' AND token=?", (auth,))
        raise
    return {'identity_matched': True, 'invitation_already_registered': True,
            'auth_token_restored': not exists, 'encrypted_checkpoint_verified': True,
            'mail_io_started': False, 'identity_replaced': False}


def run() -> dict:
    revision = os.getenv('INVITATION_REPAIR_REVISION', '')
    if (not re.fullmatch('[0-9a-f]{40}', revision) or revision != os.getenv('RUNNING_SHA')
            or os.getenv('STATE_BACKUP_REQUIRED') != '1' or os.getenv('OWNER_ONLY') != '0'
            or os.getenv('INVITE_AUTO_ACCESS') != '1' or os.getenv('REVOKE_LEGACY_MINIAPPS') != '1'):
        raise ValueError('invitation repair is not armed for this revision')
    import state_backup
    from michiflin.identity_guard import _fingerprint_from_key
    return repair(Path(os.environ['CONFIG_DIR']), os.environ['INVITATION_REPAIR_LINK'],
                  state_backup.save, _fingerprint_from_key)


if __name__ == '__main__':
    import json
    try:
        print('SAFE_INVITATION_REPAIR ' + json.dumps(run(), sort_keys=True))
    except Exception as exc:
        print('SAFE_INVITATION_REPAIR_FAILED ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
