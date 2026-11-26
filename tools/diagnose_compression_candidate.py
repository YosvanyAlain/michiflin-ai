#!/usr/bin/env python3
"""Try XZ solely on an isolated restored snapshot and emit aggregate numbers.

No cloud backup is written and no mail is opened. The temporary extraction is
removed automatically. The comparison hashes files and database rows privately;
no file names, hashes, addresses, messages or data leave this process.
"""
from __future__ import annotations
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import state_backup as backup  # noqa: E402

CORE_TABLES = ('msgs', 'msgs_status_updates', 'contacts', 'chats', 'config', 'keypairs', 'imap', 'smtp')


def digest_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.digest()


def files(root: Path, db_relative: Path) -> dict[str, tuple[int, int, bytes]]:
    value: dict[str, tuple[int, int, bytes]] = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError('unsafe_symlink')
        if path.is_file() and path.relative_to(root) != db_relative:
            info = path.stat()
            value[str(path.relative_to(root))] = (info.st_size, stat.S_IMODE(info.st_mode), digest_file(path))
    return value


def row_digest(row: tuple) -> bytes:
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            tag, raw = b'n', b''
        elif isinstance(value, bytes):
            tag, raw = b'b', value
        elif isinstance(value, str):
            tag, raw = b's', value.encode('utf8')
        elif type(value) is int:
            tag, raw = b'i', str(value).encode('ascii')
        elif type(value) is float:
            tag, raw = b'f', value.hex().encode('ascii')
        else:
            raise ValueError('unsupported_database_value')
        digest.update(tag + len(raw).to_bytes(8, 'big') + raw)
    return digest.digest()


def database_fingerprint(path: Path) -> tuple[tuple[tuple[str, str], ...], dict[str, tuple[int, bytes]]]:
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)) as db:
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ValueError('database_integrity_failed')
        schema = tuple(sorted((str(name), str(sql or '')) for name, sql in
                              db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")))
        names = {name for name, _ in schema}
        if not {'msgs', 'msgs_status_updates', 'contacts', 'chats', 'config'}.issubset(names):
            raise ValueError('core_schema_missing')
        result = {}
        for name in CORE_TABLES:
            if name not in names:
                continue
            # The table names here are fixed constants, never user-provided.
            rows = sorted(row_digest(tuple(row)) for row in db.execute('SELECT * FROM "' + name + '"'))
            h = hashlib.sha256()
            for item in rows:
                h.update(item)
            result[name] = (len(rows), h.digest())
        return schema, result


def encrypted_size(compressed_bytes: int) -> int:
    """Exact length of MAGIC + salt + Fernet's padded, base64-encoded token."""
    padded = 16 * (compressed_bytes // 16 + 1)
    token_raw = 1 + 8 + 16 + padded + 32
    return len(backup.MAGIC) + backup.SALT_BYTES + 4 * ((token_raw + 2) // 3)


def rehearsal(root: Path, runner_temp: Path, original_bytes: int) -> dict[str, int | bool]:
    databases = [p for p in (root / 'accounts').rglob('dc.db') if p.is_file() and not p.is_symlink()]
    if len(databases) != 1:
        raise ValueError('single_account_required')
    db_relative = databases[0].relative_to(root)
    before_files = files(root, db_relative)
    before_db = database_fingerprint(databases[0])
    previous = {k: os.environ.get(k) for k in ('STATE_BACKUP_COMPRESSION', 'STATE_BACKUP_INCLUDE_BLOBS', 'STATE_BACKUP_INCLUDE_MEDIA')}
    try:
        os.environ.update(STATE_BACKUP_COMPRESSION='xz', STATE_BACKUP_INCLUDE_BLOBS='1', STATE_BACKUP_INCLUDE_MEDIA='1')
        started = time.monotonic()
        compressed = backup.archive(root)
        duration = int(time.monotonic() - started)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if not compressed.startswith(b'\xfd7zXZ\x00'):
        raise ValueError('xz_format_missing')
    backup._verify_plaintext(compressed)
    backup._verify_clean_origin(compressed)
    legacy_rejected = False
    try:
        with tarfile.open(fileobj=io.BytesIO(compressed), mode='r:gz'):
            pass
    except tarfile.ReadError:
        legacy_rejected = True
    with tempfile.TemporaryDirectory(prefix='compression-rehearsal-', dir=runner_temp) as temporary:
        target = Path(temporary) / 'restored'
        backup._install_restored(compressed, target)
        after_files = files(target, db_relative)
        after_db = database_fingerprint(target / db_relative)
    projected = encrypted_size(len(compressed))
    return {'schema': 1, 'read_only': True, 'mail_io_started': False,
            'backup_uploaded': False, 'source_snapshot_bytes': original_bytes,
            'xz_plaintext_bytes': len(compressed), 'xz_encrypted_bytes_estimate': projected,
            'xz_headroom_bytes': max(0, backup.MAX_RELEASE_BACKUP_BYTES - projected),
            'xz_would_fit': projected <= backup.MAX_RELEASE_BACKUP_BYTES,
            'compression_seconds': duration, 'compared_regular_files': len(before_files),
            'all_selected_nondatabase_files_identical': before_files == after_files,
            'core_database_tables_identical': before_db == after_db,
            'database_quick_check_passed': True, 'clean_origin_verified': True,
            'legacy_gzip_only_reader_rejects_new_format': legacy_rejected}


def main() -> None:
    runner_temp = Path(os.environ['RUNNER_TEMP'])
    trace = json.loads((runner_temp / 'backup-source.json').read_text())
    if trace.get('read_only') is not True or trace.get('mail_io_started') is not False or type(trace.get('bytes')) is not int:
        raise ValueError('invalid_source_trace')
    report = rehearsal(Path(os.environ['CONFIG_DIR']), runner_temp, trace['bytes'])
    if not all(type(value) in (int, bool) for value in report.values()):
        raise ValueError('invalid_aggregate')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'compression_rehearsal_failed': True, 'error_type': type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
