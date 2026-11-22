"""Offline planning tests; never contact a backup repository or modify assets."""
from copy import deepcopy

import pytest

import state_backup as backup
from tools.plan_snapshot_protection import PRESERVED_PREFIX, plan


def inventory():
    # Synthetic old-to-new timestamps and predictable IDs; no user data.
    rows = [
        {'id': number, 'name': f'michiflin-owner-clean-state-202601{number:02}T000000Z-{number:016x}.bin',
         'state': 'uploaded', 'created_at': f'2026-01-{number:02}T00:00:00Z', 'size': number * 10}
        for number in range(1, 9)
    ]
    rows.append({'id': 99, 'name': 'unrelated.bin', 'state': 'uploaded',
                 'created_at': '2026-01-01T00:00:00Z', 'size': 10})
    return rows


def test_plan_is_read_only_reversible_and_keeps_restore_latest_and_seed(monkeypatch):
    monkeypatch.setenv('STATE_BACKUP_NAMESPACE', backup.CLEAN_OWNER_NAMESPACE)
    rows = inventory()
    before = deepcopy(rows)
    proposed = plan(rows)
    assert rows == before
    assert {x.asset_id for x in proposed} == {2, 3, 4, 5, 6}
    assert all(x.protected_name == PRESERVED_PREFIX + x.original_name for x in proposed)
    renamed = deepcopy(rows)
    for item in renamed:
        match = next((entry for entry in proposed if entry.asset_id == item['id']), None)
        if match:
            item['name'] = match.protected_name  # fixture only, not a remote PATCH
    active = [item for item in renamed if backup._managed(item)]
    assert {x['id'] for x in active} == {1, 7, 8}
    assert max(active, key=lambda item: (backup._created(item), item['id']))['id'] == 8
    assert 1 in backup.retention_ids(renamed)
    assert {x['id'] for x in renamed if not backup._managed(x)} >= {2, 3, 4, 5, 6, 99}
    for item in renamed:
        match = next((entry for entry in proposed if entry.asset_id == item['id']), None)
        if match:
            item['name'] = match.protected_name[len(PRESERVED_PREFIX):]
    assert renamed == before


def test_ambiguous_namespace_seed_listing_or_collision_aborts(monkeypatch):
    monkeypatch.setenv('STATE_BACKUP_NAMESPACE', backup.GITHUB_NATIVE_NAMESPACE)
    with pytest.raises(ValueError):
        plan(inventory())
    monkeypatch.setenv('STATE_BACKUP_NAMESPACE', backup.CLEAN_OWNER_NAMESPACE)
    with pytest.raises(ValueError):
        plan(inventory()[:2])
    duplicate = inventory(); duplicate[1]['id'] = duplicate[0]['id']
    with pytest.raises(ValueError):
        plan(duplicate)
    ambiguous = inventory(); ambiguous[0]['state'] = 'new'
    with pytest.raises(ValueError):
        plan(ambiguous)
    collision = inventory()
    collision.append({'id': 98, 'name': PRESERVED_PREFIX + collision[1]['name'],
                      'state': 'uploaded', 'created_at': '2026-01-01T00:00:00Z', 'size': 1})
    with pytest.raises(ValueError):
        plan(collision)
