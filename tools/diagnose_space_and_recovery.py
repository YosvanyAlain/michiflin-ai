#!/usr/bin/env python3
"""Offline aggregate storage inventory and two distinct backup recoveries.

The current copy was already restored by diagnostic_restore_trace.py. A second
current-namespace snapshot is downloaded, decrypted, validated and installed
in a temporary directory which is removed afterwards. No mailbox is opened,
no backup is written or deleted, and no paths, addresses, names, message text
or authentication material are emitted. Outputs are integer/bool aggregates
which the enclosing diagnostic workflow encrypts before upload.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import state_backup as backup  # noqa: E402

CATEGORIES = ('sqlite', 'webxdc', 'image', 'archive', 'document', 'audio_video', 'blob_other', 'state_other')
IMAGE = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.tif', '.tiff', '.heic', '.avif'}
ARCHIVE = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.zst', '.tgz', '.iso'}
DOCUMENT = {'.pdf', '.epub', '.txt', '.csv', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.md', '.html'}
AUDIO_VIDEO = {'.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.3gp', '.mpeg', '.mpg',
               '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.flac', '.weba'}


def file_kind(path: Path, is_blob: bool) -> str:
    suffixes = {x.lower() for x in path.suffixes}
    if path.name == 'dc.db' or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
        return 'sqlite'
    if not is_blob:
        return 'state_other'
    if '.xdc' in suffixes:
        return 'webxdc'
    if suffixes & AUDIO_VIDEO:
        return 'audio_video'
    if suffixes & IMAGE:
        return 'image'
    if suffixes & ARCHIVE:
        return 'archive'
    if suffixes & DOCUMENT:
        return 'document'
    return 'blob_other'


def blob_references(databases: list[Path]) -> dict[str, set[str]]:
    """Classify DB references, without emitting any paths or message data.

    The reference is conservative: any unknown/received/unsettled reference
    takes precedence over delivered-only in the output. No category is a
    recommendation or authorization to delete the underlying blob.
    """
    result: dict[str, set[str]] = {}
    if len(databases) != 1:
        raise ValueError('single_account_database_required')
    db_path = databases[0]
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=30)) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(msgs)')}
        if not {'param', 'type', 'from_id', 'state', 'deleted'}.issubset(columns):
            raise ValueError('message_schema_unrecognized')
        for param, kind, sender, state, deleted in db.execute(
                'SELECT param,type,from_id,state,deleted FROM msgs WHERE deleted=0 AND param IS NOT NULL'):
            if deleted or not isinstance(param, str):
                continue
            names = re.findall(r'(?:^|\n)f=\$BLOBDIR/([^\r\n]+)', param)
            for name in names:
                # Refuse ambiguous/malicious names, never turn them into a path.
                if not name or Path(name).name != name:
                    continue
                if kind == 80:
                    category = 'webxdc'
                elif sender != 1:
                    category = 'incoming_or_unknown'
                elif state in (18, 20, 24):
                    category = 'outgoing_unsettled'
                elif state in (26, 28):
                    category = 'outgoing_delivered'
                else:
                    category = 'outgoing_unknown'
                result.setdefault(name, set()).add(category)
    return result


def database_space(database: Path) -> dict[str, int | bool]:
    """Group storage only by fixed internal tables; never emit schema names or content."""
    groups = {'msgs': 'messages', 'msgs_status_updates': 'webxdc_updates', 'imap': 'imap', 'smtp': 'smtp',
              'contacts': 'contacts', 'chats': 'chats', 'keypairs': 'keys', 'config': 'config'}
    result: dict[str, int | bool] = {'dbstat_available': False, 'sqlite_freelist_bytes': 0}
    for name in (*groups.values(), 'other'):
        result['sqlite_' + name + '_bytes'] = 0
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=30)) as db:
        result['sqlite_freelist_bytes'] = db.execute('PRAGMA freelist_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
        try:
            objects = {name: table for name, table in db.execute('SELECT name,tbl_name FROM sqlite_schema')}
            pages = list(db.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name'))
        except sqlite3.OperationalError:
            return result  # Some SQLite builds lack dbstat; never guess.
        for name, amount in pages:
            category = groups.get(objects.get(name, ''), 'other')
            result['sqlite_' + category + '_bytes'] += int(amount or 0)
        result['dbstat_available'] = True
    return result


def known_download_references(root: Path, database: Path) -> set[str]:
    """Find conservative potential candidates, without excluding or deleting them.

    Only queue-recorded, non-filekit URL downloads matched by message id,
    chat, sender and delivered state qualify. Any conflicting message reference
    blocks the file. This is an inventory, not proof of every possible DB use.
    """
    data = json.loads((root / 'michiflin-state.json').read_text())
    jobs = data.get('jobs') if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        raise ValueError('queue_metadata_invalid')
    claimed: dict[int, set[int]] = {}
    for job in jobs.values():
        if (not isinstance(job, dict) or job.get('filekit_local') or job.get('kind') not in ('file', 'video', 'audio')
                or not isinstance(job.get('url'), str) or not re.match(r'^https?://', job['url'], flags=re.I)
                or type(job.get('chat')) is not int or job['chat'] <= 9 or not isinstance(job.get('messages'), list)):
            continue
        for message_id in job['messages']:
            if type(message_id) is int and message_id > 0:
                claimed.setdefault(message_id, set()).add(job['chat'])
    good: set[str] = set()
    bad: set[str] = set()
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=30)) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(msgs)')}
        if not {'id', 'param', 'type', 'from_id', 'state', 'deleted', 'chat_id'}.issubset(columns):
            raise ValueError('message_schema_unrecognized')
        for mid, param, kind, sender, status, deleted, chat_id in db.execute(
                'SELECT id,param,type,from_id,state,deleted,chat_id FROM msgs WHERE param IS NOT NULL'):
            if not isinstance(param, str):
                continue
            for name in re.findall(r'(?:^|\n)f=\$BLOBDIR/([^\r\n]+)', param):
                if not name or Path(name).name != name:
                    continue
                valid = (not deleted and sender == 1 and kind != 80 and status in (26, 28)
                         and claimed.get(mid) == {chat_id} and '.xdc' not in {x.lower() for x in Path(name).suffixes})
                (good if valid else bad).add(name)
    return good - bad


def file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.digest()


def footprint(root: Path) -> dict[str, int | bool]:
    accounts = root / 'accounts'
    databases = [p for p in accounts.rglob('dc.db') if p.is_file() and not p.is_symlink()]
    references = blob_references(databases)
    possible_downloads = known_download_references(root, databases[0])
    seen_hashes: set[tuple[int, bytes]] = set()
    report: dict[str, int | bool] = {'schema': 1, 'read_only': True, 'mail_io_started': False,
                                    'regular_file_count': 0, 'uncompressed_file_bytes': 0,
                                    'account_database_count': len(databases),
                                    'known_delivered_url_download_count': 0, 'known_delivered_url_download_bytes': 0,
                                    'duplicate_blob_count': 0, 'duplicate_blob_bytes': 0,
                                    'duplicate_webxdc_count': 0, 'duplicate_webxdc_bytes': 0}
    report.update(database_space(databases[0]))
    for category in CATEGORIES:
        report['kind_' + category + '_count'] = 0
        report['kind_' + category + '_bytes'] = 0
    for category in ('webxdc_reference', 'incoming_or_unknown_reference', 'outgoing_unsettled_reference',
                     'outgoing_delivered_only_reference', 'outgoing_unknown_reference', 'no_direct_msg_reference'):
        report['ref_' + category + '_count'] = 0
        report['ref_' + category + '_bytes'] = 0
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('symlink_in_restored_snapshot')
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size < 0:
            raise ValueError('invalid_file_size')
        relative = path.relative_to(root)
        blob = any(part.endswith('-blobs') for part in relative.parts[:-1])
        category = file_kind(path, blob)
        report['regular_file_count'] += 1
        report['uncompressed_file_bytes'] += size
        report['kind_' + category + '_count'] += 1
        report['kind_' + category + '_bytes'] += size
        if blob:
            if path.name in possible_downloads:
                report['known_delivered_url_download_count'] += 1
                report['known_delivered_url_download_bytes'] += size
            fingerprint = (size, file_digest(path))
            if fingerprint in seen_hashes:
                report['duplicate_blob_count'] += 1
                report['duplicate_blob_bytes'] += size
                if category == 'webxdc':
                    report['duplicate_webxdc_count'] += 1
                    report['duplicate_webxdc_bytes'] += size
            else:
                seen_hashes.add(fingerprint)
            flags = references.get(path.name, set())
            if 'webxdc' in flags:
                relation = 'webxdc_reference'
            elif 'incoming_or_unknown' in flags:
                relation = 'incoming_or_unknown_reference'
            elif 'outgoing_unsettled' in flags:
                relation = 'outgoing_unsettled_reference'
            elif 'outgoing_unknown' in flags:
                relation = 'outgoing_unknown_reference'
            elif 'outgoing_delivered' in flags:
                relation = 'outgoing_delivered_only_reference'
            else:
                relation = 'no_direct_msg_reference'
            report['ref_' + relation + '_count'] += 1
            report['ref_' + relation + '_bytes'] += size
    return report


def verify_second(root: Path, source: dict[str, Any]) -> dict[str, int | bool]:
    if os.getenv('STATE_BACKUP_NAMESPACE') != backup.CLEAN_OWNER_NAMESPACE:
        raise ValueError('clean_namespace_required')
    cfg = backup.configuration()
    if not cfg:
        raise ValueError('backup_configuration_missing')
    token, repo, passphrase = cfg
    backup._assert_private(token, repo)
    release = backup._release(token, repo)
    if release is None:
        raise ValueError('backup_release_missing')
    assets = backup._assets(token, repo, release['id'])
    snapshots = sorted((a for a in assets if backup._managed(a)),
                       key=lambda a: (backup._created(a), a['id']), reverse=True)
    primary = next((x for x in snapshots if x['id'] == source.get('asset_id')), None)
    second = next((x for x in snapshots if x['id'] != source.get('asset_id')), None)
    if primary is None or second is None or primary['id'] == second['id']:
        raise ValueError('two_distinct_snapshots_required')
    primary_sha = source.get('sha256')
    if (type(source.get('bytes')) is not int or source['bytes'] != primary.get('size')
            or not isinstance(primary_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', primary_sha)
            or primary.get('digest') != 'sha256:' + primary_sha):
        raise ValueError('primary_restore_digest_mismatch')
    keep = backup.retention_ids(assets)
    if primary['id'] not in keep or second['id'] not in keep:
        raise ValueError('verified_snapshots_outside_retention')
    encrypted = backup._download_asset(token, repo, second['id'])
    digest = hashlib.sha256(encrypted).hexdigest()
    if len(encrypted) != second.get('size') or second.get('digest') != 'sha256:' + digest:
        raise ValueError('second_snapshot_digest_mismatch')
    plaintext = backup.decrypt_backup(encrypted, passphrase)
    del encrypted, passphrase
    backup._verify_plaintext(plaintext)
    backup._verify_clean_origin(plaintext)
    with tempfile.TemporaryDirectory(prefix='verify-second-', dir=os.environ['RUNNER_TEMP']) as temporary:
        target = Path(temporary) / 'restored'
        backup._install_restored(plaintext, target)
        databases = [p for p in (target / 'accounts').rglob('dc.db') if p.is_file() and not p.is_symlink()]
        if len(databases) != 1 or not (target / backup.CLEAN_OWNER_ORIGIN).is_file():
            raise ValueError('installed_second_snapshot_incomplete')
        primary_identity = json.loads((root / backup.CLEAN_OWNER_ORIGIN).read_text())
        second_identity = json.loads((target / backup.CLEAN_OWNER_ORIGIN).read_text())
        identity_keys = ('epoch', 'owner_fingerprint', 'bot_fingerprint', 'owner_address', 'owner_chat')
        if not backup._clean_origin_valid(primary_identity) or not all(
                primary_identity.get(key) == second_identity.get(key) for key in identity_keys):
            raise ValueError('snapshot_identities_do_not_match')
        with closing(sqlite3.connect(f'file:{databases[0]}?mode=ro', uri=True, timeout=30)) as db:
            if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('installed_second_snapshot_integrity_failed')
    del plaintext
    return {'primary_asset_id': primary['id'], 'primary_bytes': primary['size'],
            'primary_digest_matches_github': True, 'primary_restored_by_diagnostic': True,
            'second_asset_id': second['id'], 'second_bytes': second['size'],
            'second_created_epoch': int(backup._created(second).timestamp()),
            'second_digest_matches_github': True, 'second_decrypted_and_installed': True,
            'both_snapshots_in_retention_set': True, 'both_snapshots_same_clean_identity': True}


def main() -> None:
    root = Path(os.environ['CONFIG_DIR'])
    trace_path = Path(os.environ['RUNNER_TEMP']) / 'backup-source.json'
    source = json.loads(trace_path.read_text())
    if (source.get('schema') != 1 or source.get('read_only') is not True or source.get('mail_io_started') is not False):
        raise ValueError('invalid_primary_trace')
    report = footprint(root)
    report.update(verify_second(root, source))
    if not all(type(value) in (int, bool) for value in report.values()):
        raise ValueError('nonaggregate_output')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Error messages may contain private file paths or repository metadata.
        print(json.dumps({'diagnostic_failed': True, 'error_type': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
