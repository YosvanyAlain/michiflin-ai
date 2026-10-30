"""Owner controls, legacy session grants, and optional verified-invite access.

Historic allow-lists never become community grants. In invitation mode, a
verified invitee's grant is durable and the emergency close is checkpointed;
maintenance remains a stricter emergency gate.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from . import config

_lock = threading.RLock()
_policy_change = threading.RLock()
_controlled = False
_close_checkpoint_ok = False
_owner_only = True
_deadline = 0.0
_guests: dict[str, str] = {}  # address -> per-grant token; re-grant never resumes old jobs
_revision = 0
_nonce = secrets.token_urlsafe(24)  # old status updates cannot reopen access after a restart
DURATIONS = {0, 15, 60, 240, 1440}  # zero: until the current process ends


def _refresh_locked() -> None:
    global _owner_only, _deadline, _revision, _nonce, _close_checkpoint_ok
    if _controlled and not _owner_only and _deadline and time.time() >= _deadline:
        _owner_only, _deadline = True, 0.0
        _close_checkpoint_ok = bool(config.INVITE_AUTO_ACCESS)
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1


def snapshot() -> dict[str, Any]:
    with _lock:
        _refresh_locked()
        enabled = _owner_only if _controlled else bool(config.OWNER_ONLY)
        return {"owner_only": enabled, "maintenance": bool(config.MAINTENANCE_MODE),
                "private_mode": enabled or bool(config.MAINTENANCE_MODE),
                "invite_auto": bool(config.INVITE_AUTO_ACCESS),
                "controlled": _controlled, "expires": _deadline, "nonce": _nonce, "revision": _revision,
                "guests": sorted(_guests) if _controlled and not enabled and not config.MAINTENANCE_MODE else []}


def preserve_history() -> bool:
    """Keep legacy data when private at boot or managed at runtime."""
    with _lock:
        return bool(config.OWNER_ONLY or config.MAINTENANCE_MODE or config.INVITE_AUTO_ACCESS or _controlled)


def valid_duration(minutes: int) -> bool:
    return type(minutes) is int and (minutes in DURATIONS or (config.INVITE_AUTO_ACCESS and minutes == -1))


def close_immediately() -> None:
    """Nonblocking emergency gate; the owner command then checkpoints it."""
    global _controlled, _owner_only, _deadline, _revision, _nonce, _close_checkpoint_ok
    with _lock:
        _controlled, _owner_only, _deadline = True, True, 0.0
        _close_checkpoint_ok = False
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1


def restore_policy() -> None:
    """Restore a checkpointed community emergency close without reviving old grants."""
    global _controlled, _owner_only, _deadline, _revision, _nonce, _close_checkpoint_ok
    if not config.INVITE_AUTO_ACCESS:
        return
    if config.OWNER_ONLY:
        with _lock:
            _controlled, _owner_only, _deadline = False, True, 0.0
            _close_checkpoint_ok = True
            _guests.clear()
            _nonce = secrets.token_urlsafe(24)
            _revision += 1
        return
    from . import state
    with state.lock:
        policy = state.data.get("invite_policy", {})
        if not isinstance(policy, dict):
            policy = {"closed": True}
        closed = policy.get("closed", False)
        if type(closed) is not bool:
            closed = True
        try:
            until = max(0.0, float(policy.get("until") or 0))
        except (TypeError, ValueError):
            closed, until = True, 0.0
    with _lock:
        _controlled = bool(policy)
        _owner_only = bool(closed and (not until or until <= time.time()))
        _deadline = until if closed and until > time.time() else 0.0
        _close_checkpoint_ok = _owner_only
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1


def set_owner_only(enabled: bool, minutes: int = 60, nonce: str = "") -> bool:
    """Close immediately; in invitation mode checkpoint the restart policy."""
    global _controlled, _owner_only, _deadline, _revision, _nonce, _close_checkpoint_ok
    if type(enabled) is not bool or not valid_duration(minutes) or not config.OWNER_ADDRESS:
        return False
    if not config.INVITE_AUTO_ACCESS:
        with _lock:
            _refresh_locked()
            if not enabled and (config.MAINTENANCE_MODE or not secrets.compare_digest(str(nonce), _nonce)):
                return False
            _controlled = True
            _owner_only = enabled
            _deadline = 0.0 if enabled or not minutes else time.time() + minutes * 60
            _guests.clear()
            _nonce = secrets.token_urlsafe(24)
            _revision += 1
        return True
    from . import community, state
    with _policy_change:
        with _lock:
            _refresh_locked()
            if not enabled and (config.MAINTENANCE_MODE or not secrets.compare_digest(str(nonce), _nonce)):
                return False
            if enabled and _controlled and _owner_only and _close_checkpoint_ok:
                return True  # the earlier immediate close already committed
            # Closing is immediate. Opening stays closed until its checkpoint is verified.
            _controlled, _owner_only, _deadline = True, True, 0.0
            _close_checkpoint_ok = False
            _guests.clear()
            _nonce = secrets.token_urlsafe(24)
            _revision += 1
            transition_revision = _revision
        until = time.time() + minutes * 60 if not enabled and minutes > 0 else 0.0
        policy = {"closed": bool(enabled or minutes != -1), "until": until}
        try:
            with state.lock:
                state.data["invite_policy"] = policy
                state.save()
            community._checkpoint()
        except Exception:
            # Keep access closed locally. A failed remote checkpoint must be
            # reported; a restart may still restore an older remote policy.
            with state.lock:
                state.data["invite_policy"] = {"closed": True, "until": 0.0}
                try:
                    state.save()
                except Exception:
                    pass
            return False
        with _lock:
            if _revision != transition_revision:
                return False  # a newer emergency close wins the race
            if enabled:
                _close_checkpoint_ok = True
            else:
                _owner_only, _deadline = False, until
        return True


def open_one_guest(address: str, minutes: int, nonce: str) -> int | None:
    """Open a session and grant only this address in one atomic transition."""
    global _controlled, _owner_only, _deadline, _revision, _nonce
    address = (address or "").strip().lower()
    if not address or config.is_owner_admin(address) or type(minutes) is not int or minutes not in DURATIONS:
        return None
    with _lock:
        _refresh_locked()
        enabled = _owner_only if _controlled else bool(config.OWNER_ONLY)
        if (not enabled or config.MAINTENANCE_MODE or not config.OWNER_ADDRESS
                or not secrets.compare_digest(str(nonce), _nonce)):
            return None
        _controlled, _owner_only = True, False
        _deadline = 0.0 if not minutes else time.time() + minutes * 60
        _guests.clear()
        _guests[address] = secrets.token_hex(16)
        _nonce = secrets.token_urlsafe(24)
        _revision += 1
        return _revision


def rollback_one_guest(address: str, expected_revision: int) -> None:
    """Close a failed single-person opening only if no one else changed it."""
    global _owner_only, _deadline, _revision, _nonce
    with _lock:
        _refresh_locked()
        if _controlled and not _owner_only and _revision == expected_revision and set(_guests) == {address}:
            _owner_only, _deadline = True, 0.0
            _guests.clear()
            _nonce = secrets.token_urlsafe(24)
            _revision += 1


def allow_guest(address: str, expected_revision: int) -> bool:
    address = (address or "").strip().lower()
    if not address or config.is_owner_admin(address):
        return False
    with _lock:
        _refresh_locked()
        if not _controlled or _owner_only or config.MAINTENANCE_MODE or expected_revision != _revision:
            return False  # an intervening close/revocation invalidates the pending grant
        if address not in _guests:
            _guests[address] = secrets.token_hex(16)
        return True


def revoke_guest(address: str) -> bool:
    global _revision
    address = (address or "").strip().lower()
    with _lock:
        _refresh_locked()
        if not _controlled or config.is_owner_admin(address):
            return False
        _guests.pop(address, None)
        _revision += 1  # also invalidate a still-pending grant of this address
        return True


def service_allowed(address: str) -> bool:
    address = (address or "").strip().lower()
    if config.is_owner_admin(address):
        return True
    with _lock:
        _refresh_locked()
        if config.MAINTENANCE_MODE or (_owner_only if _controlled else config.OWNER_ONLY):
            return False
        if not config.INVITE_AUTO_ACCESS:
            return address in _guests if _controlled else True  # legacy gate remains separate
    # Never hold the access lock while taking state.lock: work checks take them
    # in the opposite order. Recheck closure after reading the durable registry.
    from . import community
    allowed = community.allowed(address)
    with _lock:
        _refresh_locked()
        return bool(allowed and not config.MAINTENANCE_MODE and not (_owner_only if _controlled else config.OWNER_ONLY))


def revision(address: str) -> int:
    address = (address or "").strip().lower()
    if config.is_owner_admin(address):
        return 0
    if config.INVITE_AUTO_ACCESS:
        from . import community
        token = community.stamp(address)
        with _lock:
            _refresh_locked()
            # A closure invalidates running work; the stable stamp still lets
            # eligible jobs resume after the owner reopens the service.
            return (int(token, 16) if token else 0) ^ (_revision & ((1 << 128) - 1))
    with _lock:
        _refresh_locked()
        return int(_guests.get(address, "0"), 16) if _controlled else _revision


def session_stamp(address: str) -> str:
    address = (address or "").strip().lower()
    if config.INVITE_AUTO_ACCESS and not config.is_owner_admin(address):
        from . import community
        return community.stamp(address) if service_allowed(address) else ""
    with _lock:
        _refresh_locked()
        return _guests.get(address, "") if _controlled and service_allowed(address) else ""


def bound_allowed(item: dict[str, Any]) -> bool:
    """Never revive a guest's historical jobs or app instances after a new grant."""
    address = str(item.get("addr") or f"chat:{item.get('chat')}").strip().lower()
    if not service_allowed(address):
        return False
    if config.INVITE_AUTO_ACCESS and not config.is_owner_admin(address):
        stamp = session_stamp(address)
        return bool(stamp) and item.get("access_session") == stamp
    with _lock:
        _refresh_locked()
        if _controlled and not config.is_owner_admin(address):
            stamp = _guests.get(address, "")
            return bool(stamp) and item.get("access_session") == stamp
        return True


def job_allowed(job: dict[str, Any]) -> bool:
    """A sealed identity never resumes a previous identity's chat/message IDs."""
    epoch = job.get("epoch")
    if config.BOT_EPOCH_STRICT and epoch != config.BOT_EPOCH:
        return False
    return bound_allowed(job)


def reset() -> None:
    """Return to environment defaults, as on process restart (also useful for isolated tests)."""
    global _controlled, _owner_only, _deadline, _revision, _nonce, _close_checkpoint_ok
    with _lock:
        _controlled, _owner_only, _deadline = False, True, 0.0
        _close_checkpoint_ok = False
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1
