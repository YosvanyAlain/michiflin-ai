"""Aggregate, read-only backup inventory. All metadata and credentials are synthetic."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import state_backup as backup
from tools.diagnose_backup_inventory import inventory


def test_snapshot_inventory_projects_rotation_without_modifying_assets(monkeypatch):
    monkeypatch.delenv('STATE_BACKUP_NAMESPACE', raising=False)
    now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    def item(number, moment, prefix='michiflin-state-', size=100):
        return {'id': number, 'state': 'uploaded', 'size': size,
                'created_at': moment.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'name': prefix + moment.strftime('%Y%m%dT%H%M%SZ') + f'-{number:016x}.bin'}
    items = [item(i + 1, now - timedelta(minutes=i + 1)) for i in range(7)]
    items += [item(20, now - timedelta(days=4), prefix='michiflin-pending-')]
    items += [{'id': 99, 'name': 'other-private-file', 'state': 'uploaded', 'size': 5}]
    original = json.dumps(items, sort_keys=True)
    monkeypatch.setattr(backup, 'configuration', lambda: ('synthetic-token', 'synthetic/private', 'synthetic-passphrase'))
    private = Mock(); monkeypatch.setattr(backup, '_assert_private', private)
    monkeypatch.setattr(backup, '_release', lambda *_: {'id': 17})
    monkeypatch.setattr(backup, '_assets', lambda *_: items)
    forbidden = Mock(side_effect=AssertionError('write operation attempted'))
    monkeypatch.setattr(backup.requests, 'post', forbidden)
    monkeypatch.setattr(backup.requests, 'patch', forbidden)
    monkeypatch.setattr(backup.requests, 'delete', forbidden)
    report = inventory(now)
    assert report == {'schema': 1, 'read_only': True, 'backup_repo_private': True,
                      'release_asset_count': 9, 'current_namespace_snapshots': 7,
                      'current_namespace_snapshot_bytes': 700, 'current_namespace_old_pending': 1,
                      'existing_snapshots_outside_next_retention_set': 2}
    assert json.dumps(items, sort_keys=True) == original
    private.assert_called_once()
    forbidden.assert_not_called()
    serialized = json.dumps(report)
    assert 'synthetic' not in serialized and 'other-private-file' not in serialized


def test_inventory_fails_closed_when_backup_cannot_be_inspected(monkeypatch):
    monkeypatch.setattr(backup, 'configuration', lambda: None)
    with pytest.raises(RuntimeError):
        inventory()
    monkeypatch.setattr(backup, 'configuration', lambda: ('token', 'owner/private', 'passphrase'))
    monkeypatch.setattr(backup, '_assert_private', lambda *_: None)
    monkeypatch.setattr(backup, '_release', lambda *_: None)
    with pytest.raises(RuntimeError):
        inventory()
