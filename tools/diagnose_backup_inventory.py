#!/usr/bin/env python3
"""Read-only, aggregate inventory of the configured encrypted primary backup.

No credentials, asset names, identifiers, timestamps, paths or user data are
emitted. The projection describes eligibility, not a promise that rotation
will actually run; it is stale as soon as another snapshot is created.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import state_backup as backup  # noqa: E402


def inventory(now: datetime | None = None) -> dict[str, int | bool]:
    settings = backup.configuration()
    if not settings:
        raise RuntimeError('backup configuration is missing')
    token, repo, _passphrase = settings
    backup._assert_private(token, repo)
    release = backup._release(token, repo)
    if release is None:
        raise RuntimeError('backup release is missing')
    items = backup._assets(token, repo, release['id'])
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone-aware timestamp required')
    now = now.astimezone(timezone.utc)
    current, pending, _pattern = backup._backup_names()
    snapshots = [item for item in items if backup._managed(item)]
    old_pending = [item for item in items if backup._managed(item, pending)
                   and backup._created(item) < now - timedelta(days=2)]
    next_id = max((item['id'] for item in items if type(item.get('id')) is int), default=0) + 1
    hypothetical = {'id': next_id, 'name': current + now.strftime('%Y%m%dT%H%M%SZ') + '-0000000000000000.bin',
                    'state': 'uploaded', 'created_at': now.strftime('%Y-%m-%dT%H:%M:%SZ')}
    keep = backup.retention_ids(items + [hypothetical])
    def size(item: dict) -> int:
        value = item.get('size')
        return value if type(value) is int and value >= 0 else 0
    result = {
        'schema': 1,
        'read_only': True,
        'backup_repo_private': True,
        'release_asset_count': len(items),
        'current_namespace_snapshots': len(snapshots),
        'current_namespace_snapshot_bytes': sum(map(size, snapshots)),
        'current_namespace_old_pending': len(old_pending),
        'existing_snapshots_outside_next_retention_set': sum(item['id'] not in keep for item in snapshots),
    }
    assert all(type(value) in (bool, int) for value in result.values())
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(inventory(), sort_keys=True, indent=2))
    except Exception as exc:
        # Never print exception messages: URLs can include private repository data.
        print(json.dumps({'inventory_failed': True, 'error_type': type(exc).__name__}))
        raise SystemExit(1)
