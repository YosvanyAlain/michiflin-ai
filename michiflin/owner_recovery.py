"""Explicit, revision-pinned recovery delivery to the already verified owner only.

This does not open access, restore old accounts, delete apps or replay work.
"""
from __future__ import annotations

import json
import logging
import os
import re

from . import chat, config, identity_guard, miniapp, state

log = logging.getLogger('michiflin')


def deliver() -> bool:
    requested = os.getenv('OWNER_APP_RECOVERY_REVISION', '').strip()
    running = os.getenv('RUNNING_SHA', '').strip()
    if (not re.fullmatch(r'[0-9a-f]{40}', requested) or requested != running
            or not config.OWNER_ONLY or not config.BOT_EPOCH_STRICT
            or not config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY
            or not chat.ready()):
        return False
    try:
        candidates = [config.CONFIG_DIR / '.owner-clean-origin-v1.json',
                      config.CONFIG_DIR / '.verified-identity-v1.json']
        marker_path = next((p for p in candidates if p.is_file()), None)
        if marker_path is None:
            return False
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        fingerprint = identity_guard.normalize_fingerprint(marker.get('owner_fingerprint', ''))
        if (marker.get('confirmed') is not True or marker.get('epoch') != config.BOT_EPOCH
                or str(marker.get('owner_address') or '').casefold() != config.OWNER_ADDRESS.casefold()
                or not fingerprint
                or fingerprint != identity_guard.normalize_fingerprint(config.OWNER_PGP_FINGERPRINT)):
            return False
        chat_id = marker.get('owner_chat')
        if type(chat_id) is not int or chat_id <= 9:
            return False
        entry = {'chat': chat_id, 'addr': config.OWNER_ADDRESS}
        if not identity_guard.authorized_entry(chat.rpc(), chat.account(), entry):
            return False
        # Serialize against ordinary /app delivery and first welcome. Existing
        # instances and their independent local data remain registered.
        with miniapp._upgrade_lock:
            with state.lock:
                already_sent = any(isinstance(e, dict) and e.get('recovery_revision') == requested
                                   and e.get('epoch') == config.BOT_EPOCH
                                   and str(e.get('addr') or '').casefold() == config.OWNER_ADDRESS.casefold()
                                   for e in miniapp._apps().values())
            if already_sent:
                return False
            mid = miniapp.send_app(chat_id, 'es', updated=True, address=config.OWNER_ADDRESS,
                                   recovery_revision=requested)
        if mid:
            log.info('Explicit owner recovery app registered; previous instances preserved')
        return bool(mid)
    except Exception as exc:
        # Never expose the marker, identity, exception message or private paths.
        log.warning('Owner app recovery deferred (%s)', type(exc).__name__)
        return False
