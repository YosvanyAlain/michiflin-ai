"""Select recipients for an explicitly authorized, one-time miniapp release.

Only existing app users with a current access grant and the matching, verified
one-to-one encrypted chat are considered. This module never sends, starts mail
I/O, creates contacts, rotates keys or exports addresses to diagnostics.
"""
from __future__ import annotations

import copy
from typing import Any

from . import access, admin, config, identity_guard, state


def _time(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _chat_only(address: str, chat_id: int) -> bool:
    """Respect the person's current explicit chat-only choice, if any."""
    with state.lock:
        choices = state.data.get('chat_interfaces', {})
        choice = choices.get(f'{address}:{chat_id}') if isinstance(choices, dict) else None
    return bool(isinstance(choice, dict) and choice.get('chat') == chat_id
                and choice.get('epoch') == config.BOT_EPOCH
                and choice.get('grant') == access.session_stamp(address)
                and choice.get('mode') == 'full')


def eligible(rpc: Any, account: int, entry: dict[str, Any]) -> bool:
    """Revalidate identity, grant, live policy and the exact individual chat."""
    try:
        address = str(entry.get('addr') or '').strip().casefold()
        chat_id = int(entry.get('chat') or 0)
        if (not address or chat_id <= 9 or entry.get('epoch') != config.BOT_EPOCH
                or not access.bound_allowed(entry) or not admin.is_allowed(address)
                or state.get_kill_switch(address) or _chat_only(address, chat_id)):
            return False
        contact_id = int(rpc.lookup_contact_id_by_addr(account, address) or 0)
        if not contact_id or int(rpc.get_chat_id_by_contact_id(account, contact_id) or 0) != chat_id:
            return False
        return bool(identity_guard.authorized_chat(rpc, account, chat_id, address, contact_id=contact_id))
    except Exception:
        return False


def select(rpc: Any, account: int, current_build: str) -> list[dict[str, Any]]:
    """At most one historical binding per eligible person; omit current apps.

    The hard gates intentionally match the reviewed private-invitation release.
    Old/unknown grants, group chats, key changes, chat-only choices and uncertain
    current-build registrations never become new permission to send.
    """
    if (not config.INVITE_AUTO_ACCESS or not config.BOT_VERIFIED_CONTACTS_ONLY
            or config.EMAIL_KEY_RECOVERY or not config.MINIAPP_CURRENT_ONLY
            or config.MAINTENANCE_MODE or state.maintenance_active() or not current_build):
        return []
    with state.lock:
        apps = state.data.get('apps', {})
        if not isinstance(apps, dict):
            return []
        candidates = [copy.deepcopy(entry) for entry in apps.values() if isinstance(entry, dict)]
    latest: dict[str, dict[str, Any]] = {}
    already_current: set[str] = set()
    for entry in candidates:
        if not eligible(rpc, account, entry):
            continue
        address = str(entry['addr']).strip().casefold()
        if entry.get('build') == current_build:
            already_current.add(address)
        previous = latest.get(address)
        if previous is None or _time(entry.get('sent')) > _time(previous.get('sent')):
            latest[address] = entry
    return [latest[address] for address in sorted(latest) if address not in already_current]
