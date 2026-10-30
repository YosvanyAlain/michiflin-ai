"""Automatic, key-bound admission following a completed private SecureJoin.

An invitation is a bearer capability: do not log, publish or persist its URI.
An address or an old allow-list entry alone never grants community access.
"""
from __future__ import annotations

import copy
import hmac
import os
import re
import secrets
import threading
import time
from typing import Any

from . import config, state

# Transitions are serialized without holding state.lock during network backup.
# Readers ignore a provisional grant until its required checkpoint is verified.
_transition = threading.RLock()
_pending: set[str] = set()
_STAMP = re.compile(r"^[0-9a-f]{32}$")
_KEY = re.compile(r"^[0-9a-f]{64}$")
_ADDRESS = re.compile(r"[^\s@]{1,128}@[^\s@]{1,255}")


class CheckpointError(RuntimeError):
    """A change could not be committed to the required encrypted backup."""


def _address(value: str) -> str:
    return str(value or "").strip().casefold()


def _records() -> dict[str, Any]:
    records = state.data.setdefault("invitees", {})
    if not isinstance(records, dict):
        raise ValueError("invalid invitee registry")
    return records


def _checkpoint() -> None:
    if os.getenv("STATE_BACKUP_REQUIRED", "0").strip() == "1":
        import state_backup
        state_backup.save(rotate=False)


def record(address: str) -> dict[str, Any] | None:
    """An independent snapshot; provisional, malformed and inactive grants fail closed."""
    address = _address(address)
    with state.lock:
        if address in _pending:
            return None
        entry = _records().get(address)
        if (not isinstance(entry, dict) or entry.get("active") is not True or entry.get("banned") is True
                or not _STAMP.fullmatch(str(entry.get("stamp") or ""))
                or not _KEY.fullmatch(str(entry.get("key") or ""))):
            return None
        try:
            if int(entry.get("chat") or 0) <= 9:
                return None
        except (TypeError, ValueError):
            return None
        return dict(entry)


def allowed(address: str) -> bool:
    return bool(record(address))


def stamp(address: str) -> str:
    entry = record(address)
    return str(entry["stamp"]) if entry else ""


def addresses() -> list[str]:
    with state.lock:
        names = list(_records())
    return sorted(name for name in names if record(name))


def blocked_addresses() -> list[str]:
    with state.lock:
        return sorted(name for name, entry in _records().items()
                      if isinstance(entry, dict) and entry.get("banned") is True)


def matches(address: str, chat_id: int, fingerprint: str) -> bool:
    from . import identity_guard
    entry = record(address)
    digest = identity_guard._key_digest(fingerprint)
    try:
        return bool(entry and digest and int(entry["chat"]) == int(chat_id)
                    and hmac.compare_digest(str(entry["key"]), digest))
    except (TypeError, ValueError):
        return False


def admit_from_securejoin(rpc: Any, account: int, contact_id: int) -> bool:
    """Called only for the core's successful inviter-progress=1000 event.

    Also validate a directly verified key, an encrypted single chat, the
    address and the current access mode. Key changes need another successful
    SecureJoin; administrator email recovery never applies to invitees.
    """
    if not config.INVITE_AUTO_ACCESS or config.MAINTENANCE_MODE:
        return False
    from . import access, identity_guard
    contact = rpc.get_contact(account, int(contact_id))
    address = _address(getattr(contact, "address", ""))
    if (not _ADDRESS.fullmatch(address) or config.is_owner_admin(address)
            or getattr(contact, "is_verified", None) is not True or getattr(contact, "verifier_id", None) != 1
            or getattr(contact, "is_key_contact", None) is not True or getattr(contact, "e2ee_avail", None) is not True):
        return False
    chat_id = int(rpc.create_chat_by_contact_id(account, int(contact_id)))
    info = rpc.get_basic_chat_info(account, chat_id)
    kind = getattr(info.chat_type, "value", info.chat_type)
    members = rpc.get_chat_contacts(account, chat_id)
    if (chat_id <= 9 or str(kind) != "Single" or info.is_encrypted is not True or info.is_self_talk is not False
            or len(members) != 1 or int(members[0]) != int(contact_id)):
        return False
    # SecureJoin, not a mere incoming message, is the reason to accept a request.
    if getattr(info, "is_contact_request", False):
        rpc.accept_chat(account, chat_id)
        info = rpc.get_basic_chat_info(account, chat_id)
        if info.is_contact_request is not False:
            return False
    fingerprint = identity_guard.contact_fingerprint(rpc, account, int(contact_id))
    digest = identity_guard._key_digest(fingerprint)
    if not digest:
        return False
    with _transition:
        if access.snapshot()["private_mode"]:
            return False
        with state.lock:
            records = _records()
            previous = copy.deepcopy(records.get(address))
            if previous is not None and not isinstance(previous, dict):
                return False
            if previous and previous.get("banned") is True:
                return False
            if (previous and previous.get("active") is True and previous.get("key") == digest
                    and int(previous.get("chat") or 0) == chat_id and _STAMP.fullmatch(str(previous.get("stamp") or ""))):
                return True
            now = time.time()
            first = float((previous or {}).get("first_join") or now)
            _pending.add(address)
            records[address] = {"active": True, "banned": False, "chat": chat_id, "key": digest,
                                "stamp": secrets.token_hex(16), "first_join": first, "last_join": now}
            try:
                state.save()
            except Exception:
                if previous is None:
                    records.pop(address, None)
                else:
                    records[address] = previous
                _pending.discard(address)
                raise
        try:
            _checkpoint()
        except Exception as exc:
            with state.lock:
                if previous is None:
                    records.pop(address, None)
                else:
                    records[address] = previous
                _pending.discard(address)
                try:
                    state.save()
                except Exception:
                    pass
            raise CheckpointError("invite checkpoint failed; admission postponed") from exc
        with state.lock:
            _pending.discard(address)
        return True


def revoke(address: str, *, block: bool = False) -> bool:
    """Invalidate a grant without deleting messages, jobs or personal data."""
    address = _address(address)
    if not _ADDRESS.fullmatch(address) or config.is_owner_admin(address):
        return False
    with _transition:
        with state.lock:
            records = _records()
            old = records.get(address)
            if old is not None and not isinstance(old, dict):
                return False
            if old is None and not block:
                return False
            item = dict(old or {})
            item.update(active=False, banned=bool(block), stamp=secrets.token_hex(16))
            records[address] = item
            state.save()
        try:
            _checkpoint()
        except Exception as exc:
            # Keep the immediate denial; do not pretend the remote checkpoint
            # succeeded. A restart could otherwise restore an older grant.
            raise CheckpointError("revocation could not be backed up") from exc
    return True


def unblock(address: str) -> bool:
    """Remove an explicit ban; a new verified SecureJoin is still required."""
    address = _address(address)
    if not _ADDRESS.fullmatch(address) or config.is_owner_admin(address):
        return False
    with _transition:
        with state.lock:
            records = _records()
            old = records.get(address)
            if isinstance(old, dict):
                item = dict(old)
                item.update(active=False, banned=False, stamp=secrets.token_hex(16))
                records[address] = item
            state.save()
        try:
            _checkpoint()
        except Exception as exc:
            raise CheckpointError("unblock could not be backed up") from exc
    return True
