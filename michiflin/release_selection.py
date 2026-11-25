"""Private, read-only recipient selection for an owner-requested release.

A current SecureJoin grant (or configured administrator) and an existing,
verified, encrypted individual chat are mandatory. This does not create
contacts, learn keys, renew grants, change preferences or send messages.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from . import access, admin, community, config, identity_guard, state


@dataclass(frozen=True)
class Candidate:
    address: str
    chat_id: int
    contact_id: int
    grant: str
    fingerprint: str
    locale: str
    already_current: bool


def _locale(address: str, chat_id: int) -> str:
    # Prefer a language actually observed from this person's current grant;
    # never inherit the owner's language or guess from their address.
    observed: list[tuple[float, str]] = []
    with state.lock:
        apps = state.data.get('apps', {})
        entries = list(apps.values()) if isinstance(apps, dict) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('epoch') != config.BOT_EPOCH:
            continue
        try:
            age = float(entry.get('active') or 0)
            if (str(entry.get('addr') or '').strip().casefold() == address and int(entry.get('chat') or 0) == chat_id
                    and entry.get('language_observed') is True and entry.get('locale') in {'es', 'en'}
                    and math.isfinite(age) and age > 0 and access.bound_allowed(entry)):
                observed.append((age, str(entry['locale'])))
        except (ValueError, TypeError, OverflowError):
            continue
    return max(observed, default=(0.0, 'es'))[1]


def _already_current(address: str, chat_id: int, current_build: str) -> bool:
    """A known current registration suppresses an automatic duplicate.

    A missing/unreadable message is not evidence that it was never delivered.
    """
    with state.lock:
        apps = state.data.get('apps', {})
        entries = list(apps.values()) if isinstance(apps, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            if (str(entry.get('addr') or '').strip().casefold() == address and int(entry.get('chat') or 0) == chat_id
                    and entry.get('epoch') == config.BOT_EPOCH and entry.get('build') == current_build
                    and access.bound_allowed(entry)):
                return True
        except (ValueError, TypeError, OverflowError):
            continue
    return False


def candidate(rpc: Any, account: int, address: str, current_build: str) -> Candidate | None:
    """Recheck the exact current grant and key; uncertainty excludes the target."""
    address = str(address or '').strip().casefold()
    if not address or not current_build:
        return None
    try:
        if (config.MAINTENANCE_MODE or state.maintenance_active() or not admin.is_allowed(address)
                or admin.is_blocked(address) or state.get_kill_switch(address)):
            return None
        is_admin = config.is_owner_admin(address)
        grant = access.session_stamp(address)
        entry = None if is_admin else community.record(address)
        if not is_admin and (not entry or not grant or entry.get('stamp') != grant):
            return None
        contact_id = int(rpc.lookup_contact_id_by_addr(account, address) or 0)
        chat_id = int(rpc.get_chat_id_by_contact_id(account, contact_id) or 0) if contact_id else 0
        if contact_id <= 0 or chat_id <= 9 or (entry and int(entry['chat']) != chat_id):
            return None
        blocked_ids = {int(getattr(person, 'id', 0) or 0) for person in rpc.get_blocked_contacts(account)}
        if contact_id in blocked_ids:
            return None
        fingerprint = identity_guard.contact_fingerprint(rpc, account, contact_id)
        if not fingerprint or not identity_guard.authorized_chat(rpc, account, chat_id, address, contact_id=contact_id):
            return None
        return Candidate(address, chat_id, contact_id, grant, fingerprint, _locale(address, chat_id),
                         _already_current(address, chat_id, current_build))
    except Exception:
        # No raw exception or contact detail is published to logs.
        return None


def select(rpc: Any, account: int, current_build: str) -> list[Candidate]:
    """All current private-chat users, including those without prior apps.

    Chat-only preference is not changed; the owner requested one announcement
    for each user. Revoked, banned, insecure, changed and unknown identities
    are never inferred from old app history or old address allow-lists.
    """
    if (not config.INVITE_AUTO_ACCESS or not config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY
            or not config.MINIAPP_CURRENT_ONLY or not config.REVOKE_LEGACY_MINIAPPS or not config.BOT_EPOCH_STRICT
            or config.MAINTENANCE_MODE or state.maintenance_active() or not current_build):
        return []
    addresses = set(config.owner_admin_addresses()) | set(community.addresses())
    owner = config.OWNER_ADDRESS.casefold()
    # The owner first preserves the recovery path if a later handoff is held.
    ordered = sorted(addresses, key=lambda address: (address.casefold() != owner, address))
    selected = [candidate(rpc, account, address, current_build) for address in ordered]
    return [item for item in selected if item is not None]
