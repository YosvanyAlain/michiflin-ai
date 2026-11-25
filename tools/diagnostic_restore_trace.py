#!/usr/bin/env python3
"""Restore an isolated backup and record the exact ciphertext used, privately.

This instrumented entry point is exclusively for the offline diagnostic job.
It never starts mail I/O or writes a backup. Its small output is encrypted by
that job before upload. A successful restore with zero or multiple downloads
is ambiguous and fails rather than guessing an asset from timestamps.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import state_backup  # noqa: E402


def restore_traced() -> dict[str, Any]:
    """Return the identity of the sole asset actually passed to restore()."""
    seen: list[dict[str, Any]] = []
    original = state_backup._download_asset

    def tracked(token: str, repo: str, asset_id: int) -> bytes:
        if type(asset_id) is not int or asset_id <= 0:
            raise RuntimeError('invalid_backup_asset')
        blob = original(token, repo, asset_id)
        if not isinstance(blob, bytes) or not blob.startswith(state_backup.MAGIC):
            raise RuntimeError('invalid_backup_ciphertext')
        seen.append({'asset_id': asset_id, 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()})
        return blob

    state_backup._download_asset = tracked
    try:
        state_backup.restore()
    finally:
        state_backup._download_asset = original
    if len(seen) != 1:
        raise RuntimeError('ambiguous_restored_asset')
    return {'schema': 1, 'read_only': True, 'mail_io_started': False, **seen[0]}


def write_private(path: Path, value: dict[str, Any]) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, separators=(',', ':'))
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    if os.getenv('STATE_BACKUP_NAMESPACE') != state_backup.CLEAN_OWNER_NAMESPACE:
        raise RuntimeError('clean_namespace_required')
    temp = Path(os.environ['RUNNER_TEMP'])
    # Restore's ordinary log may contain private repository metadata. It stays
    # on the isolated runner and is removed by the workflow's always step.
    with (temp / 'private-restore.log').open('w', encoding='utf-8') as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = restore_traced()
    write_private(temp / 'backup-source.json', result)
    print('ISOLATED_SNAPSHOT_RESTORED_AND_TRACED_NO_MAIL_IO')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print an exception message: it may contain private data.
        print('RESTORE_FAILED', type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
