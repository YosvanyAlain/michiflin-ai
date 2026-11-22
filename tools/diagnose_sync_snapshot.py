#!/usr/bin/env python3
"""Read-only aggregate diagnosis. Never start mail I/O or export private data."""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def diagnose(root: Path) -> dict:
    from michiflin import app_compat, app_package
    state_path = root / 'michiflin-state.json'
    data = json.loads(state_path.read_text())
    marker_path = root / '.owner-clean-origin-v1.json'
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    epoch = str(marker.get('epoch') or os.getenv('BOT_EPOCH', ''))
    owner = str(marker.get('owner_address') or os.getenv('OWNER_ADDRESS', '')).casefold()
    apps = data.get('apps', {})
    databases = list((root / 'accounts').rglob('dc.db'))
    tree = ast.parse((ROOT / 'michiflin/miniapp.py').read_text())
    files = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'APP_FILES' for t in n.targets))
    digest = hashlib.sha256(f'epoch:{epoch}'.encode())
    release = os.getenv('MINIAPP_RELEASE', '')
    strict = os.getenv('REVOKE_LEGACY_MINIAPPS', '0').lower() in {'1', 'true', 'yes', 'on'}
    if release:
        digest.update(f'miniapp-release:{release}'.encode())
    for name, source in app_package.entries(files):
        digest.update(name.encode() + (ROOT / 'webxdc' / source).read_bytes())
    current = digest.hexdigest()[:10]
    allowed = {current}
    if not strict:
        for fn in [app_compat.audit_base_build, app_compat.published_build, app_compat.latest_build,
                   app_compat.recent_build, app_compat.previous_build]:
            value = fn(epoch, str(ROOT / 'webxdc'), files)
            if value:
                allowed.add(value)
    current_only = os.getenv('MINIAPP_CURRENT_ONLY', '0').lower() in {'1', 'true', 'yes', 'on'}
    previous = app_compat.community_previous_build(epoch, release, str(ROOT / 'webxdc'), files)
    if previous and not current_only:
        allowed.add(previous)
    if current_only:
        allowed = {current}
    requested = os.getenv('OWNER_REQUESTED_APP_REVISION', '')
    requested_valid = len(requested) == 40 and all(c in '0123456789abcdef' for c in requested)
    result = {'schema': 1, 'read_only': True, 'mail_io_started': False,
              'revocation_enabled': strict or current_only, 'current_only_enabled': current_only, 'current_release_bindings': 0,
              'release_opening_checkpointed': data.get('public_access_release') == 'private-invites-20261119-v1',
              'invite_policy_open': isinstance(data.get('invite_policy'), dict) and data['invite_policy'].get('closed') is False,
              'database_count': len(databases), 'app_bindings': len(apps),
              'requested_refresh_registered': 0, 'requested_refresh_delivered': 0,
              'requested_refresh_pending': 0, 'requested_refresh_failed': 0,
              'clean_marker_present': bool(marker), 'apps_core_present': 0,
              'apps_core_missing_or_deleted': 0, 'apps_core_wrong_type': 0,
              'apps_chat_mismatch': 0, 'apps_other_epoch': 0,
              'apps_incompatible_build': 0, 'apps_owner_recipient': 0,
              'apps_missing_blob': 0, 'pending_outgoing': 0, 'failed_outgoing': 0,
              'all_core_webxdc': 0, 'unregistered_core_webxdc': 0, 'status_updates': 0}
    if len(databases) != 1:
        return result
    with sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True) as db:
        result['database_quick_check_ok'] = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        result['pending_outgoing'] = db.execute('SELECT count(*) FROM msgs WHERE state IN (18,20) AND deleted=0').fetchone()[0]
        result['failed_outgoing'] = db.execute('SELECT count(*) FROM msgs WHERE state=24 AND deleted=0').fetchone()[0]
        core_apps = {r[0] for r in db.execute('SELECT id FROM msgs WHERE type=80 AND deleted=0')}
        result['all_core_webxdc'] = len(core_apps)
        result['unregistered_core_webxdc'] = len(core_apps - {int(k) for k in apps if str(k).isdigit()})
        result['status_updates'] = db.execute('SELECT count(*) FROM msgs_status_updates').fetchone()[0]
        ages = []
        for key, entry in apps.items():
            if not str(key).isdigit() or not isinstance(entry, dict):
                continue
            result['apps_other_epoch'] += bool(entry.get('epoch') and entry.get('epoch') != epoch)
            result['apps_incompatible_build'] += (entry.get('build') not in allowed and not (not current_only and app_compat.shipped_ui_build(epoch, release, entry.get('build')))) or ((strict or current_only) and entry.get('epoch') != epoch)
            result['current_release_bindings'] += entry.get('build') == current and entry.get('epoch') == epoch
            result['apps_owner_recipient'] += str(entry.get('addr', '')).casefold() == owner
            row = db.execute('SELECT type,chat_id,deleted,param FROM msgs WHERE id=?', (int(key),)).fetchone()
            if not row or row[2]:
                result['apps_core_missing_or_deleted'] += 1
                continue
            result['apps_core_present'] += 1
            result['apps_core_wrong_type'] += row[0] != 80
            result['apps_chat_mismatch'] += row[1] != int(entry.get('chat') or 0)
            if (requested_valid and entry.get('recovery_revision') == requested
                    and entry.get('epoch') == epoch
                    and str(entry.get('addr') or '').casefold() == owner):
                result['requested_refresh_registered'] += 1
                delivery = db.execute('SELECT state FROM msgs WHERE id=?', (int(key),)).fetchone()[0]
                result['requested_refresh_delivered'] += delivery in {26, 28}
                result['requested_refresh_pending'] += delivery in {18, 20}
                result['requested_refresh_failed'] += delivery == 24
            # No file names/parameters are emitted. Only count missing local app blobs.
            for line in str(row[3] or '').splitlines():
                if line.startswith('f=$BLOBDIR/'):
                    path = databases[0].parent / 'dc.db-blobs' / line.split('/', 1)[1]
                    result['apps_missing_blob'] += not path.is_file()
            if isinstance(entry.get('sent'), (float, int)):
                ages.append(max(0, int(time.time() - entry['sent'])))
        result['newest_registered_app_age_seconds'] = min(ages) if ages else None
        result['owner_chat_present'] = bool(db.execute('SELECT 1 FROM chats WHERE id=?', (marker.get('owner_chat', -1),)).fetchone())
        fp = str(marker.get('owner_fingerprint') or '').upper()
        contacts = db.execute('SELECT fingerprint,verifier FROM contacts WHERE lower(addr)=?', (owner,)).fetchall()
        result['owner_contacts_count'] = len(contacts)
        result['owner_marker_fingerprint_present'] = any(str(f or '').upper() == fp and fp for f, _ in contacts)
        result['owner_directly_verified_contact_present'] = any(v == 1 for _, v in contacts)
    active = [e.get('active') for e in apps.values() if isinstance(e, dict) and isinstance(e.get('active'), (int, float))]
    result['latest_app_activity_age_seconds'] = max(0, int(time.time() - max(active))) if active else None
    result['snapshot_json_age_seconds'] = max(0, int(time.time() - state_path.stat().st_mtime))
    if os.getenv('SYNC_DIAGNOSTIC_OFFLINE_RPC') == '1' and marker:
        result.update(inspect_offline_core(root, marker, apps))
    # Schema intentionally permits only counts, booleans and null; no sensitive strings.
    assert all(isinstance(v, (bool, int)) or v is None for k, v in result.items() if k != 'schema')
    return result


def inspect_offline_core(root: Path, marker: dict, apps: dict) -> dict:
    """Open ONLY the isolated restored copy. Never call start_io or send methods."""
    import subprocess
    from deltachat2 import IOTransport, Rpc
    os.environ.update(BOT_EPOCH=str(marker['epoch']), BOT_EPOCH_STRICT='1',
                      BOT_VERIFIED_CONTACTS_ONLY='1', INVITE_AUTO_ACCESS='0', EMAIL_KEY_RECOVERY='0',
                      OWNER_PGP_FINGERPRINT=str(marker['owner_fingerprint']))
    from michiflin import identity_guard
    result = {'offline_owner_bindings_authorized': 0, 'offline_owner_bindings_denied': 0,
              'offline_updates_beyond_cursor': 0, 'offline_owner_chat_authorized': False}
    with IOTransport(accounts_dir=str(root / 'accounts'), stderr=subprocess.DEVNULL) as io:
        rpc = Rpc(io)
        accounts = rpc.get_all_account_ids()
        if len(accounts) != 1:
            return result
        a = accounts[0]
        result['offline_owner_chat_authorized'] = identity_guard.authorized_chat(
            rpc, a, int(marker['owner_chat']), str(marker['owner_address']))
        for key, entry in apps.items():
            if str(entry.get('addr', '')).casefold() != str(marker['owner_address']).casefold():
                continue
            authorized = identity_guard.authorized_entry(rpc, a, entry)
            result['offline_owner_bindings_authorized' if authorized else 'offline_owner_bindings_denied'] += 1
            try:
                updates = json.loads(rpc.get_webxdc_status_updates(a, int(key), int(entry.get('serial') or 0)))
                result['offline_updates_beyond_cursor'] += len(updates)
            except Exception:
                pass
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(diagnose(Path(os.environ['CONFIG_DIR'])), sort_keys=True, indent=2))
    except Exception as exc:
        print(json.dumps({'diagnostic_failed': True, 'error_type': type(exc).__name__}))
        raise SystemExit(1)
