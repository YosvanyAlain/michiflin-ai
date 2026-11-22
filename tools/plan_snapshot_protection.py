"""Pure, offline proposal for protecting older clean-owner release snapshots.

This file does not fetch, rename, upload, delete or restore any assets. Its
output is only a proposed mapping for a complete, stable inventory. A real
rename additionally requires a verified recent recovery and an exclusive safe
window with *all* writers stopped; a local lock does not stop other runners.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import state_backup as backup

PRESERVED_PREFIX = 'michiflin-preserved-v1-'


@dataclass(frozen=True)
class RenameProposal:
    asset_id: int
    original_name: str
    protected_name: str


def plan(items: list[dict[str, Any]]) -> tuple[RenameProposal, ...]:
    """Propose reversible names, retaining the two newest and oldest seed.

    Clean-owner only: never infer a seed for a different namespace. A caller
    must independently establish that the listing is complete and stable, and
    that the newest snapshot is genuinely restorable. The result is NOT proof
    that an asset has been protected; this function performs no I/O.
    """
    if os.getenv('STATE_BACKUP_NAMESPACE', '').strip() != backup.CLEAN_OWNER_NAMESPACE:
        raise ValueError('clean-owner namespace required')
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError('complete asset listing required')
    names = [item.get('name') for item in items]
    ids = [item.get('id') for item in items]
    if (any(not isinstance(name, str) or not name for name in names)
            or any(type(asset_id) is not int or asset_id <= 0 for asset_id in ids)
            or len(set(names)) != len(names) or len(set(ids)) != len(ids)):
        raise ValueError('ambiguous asset listing')
    state_prefix, _pending_prefix, _pattern = backup._backup_names()
    # If something looks like an active snapshot but is not fully recognizable,
    # abort rather than guessing whether it is a recovery candidate or the seed.
    if any(name.startswith(state_prefix) and not backup._managed(item)
           for name, item in zip(names, items)):
        raise ValueError('ambiguous managed snapshot')
    snapshots = sorted((item for item in items if backup._managed(item)),
                       key=lambda item: (backup._created(item), item['id']), reverse=True)
    if len(snapshots) < 3:
        raise ValueError('at least two newest copies and the seed must remain')
    keep = {snapshots[0]['id'], snapshots[1]['id'], snapshots[-1]['id']}
    proposals = tuple(RenameProposal(item['id'], item['name'], PRESERVED_PREFIX + item['name'])
                      for item in snapshots if item['id'] not in keep)
    if any(proposal.protected_name in names for proposal in proposals):
        raise ValueError('protected name collision')
    # Neither normal rotation nor automatic restore should recognize the new
    # names. Ensure the mapping can be reversed exactly, without any data write.
    for proposal in proposals:
        proposed_item = next(item for item in snapshots if item['id'] == proposal.asset_id).copy()
        proposed_item['name'] = proposal.protected_name
        if backup._managed(proposed_item) or not proposal.protected_name.startswith(PRESERVED_PREFIX):
            raise ValueError('unsafe protected name')
        if proposal.protected_name[len(PRESERVED_PREFIX):] != proposal.original_name:
            raise ValueError('non-reversible protected name')
    return proposals
