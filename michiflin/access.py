"""Session-scoped owner-only control and explicitly approved guests.

The environment is the boot default. A temporary override and its guest grants
live only in memory, so restarting a production deployment with OWNER_ONLY=1
always closes access again. Historic allow-lists are never reactivated by a
runtime override. Maintenance mode is an immutable, stricter emergency gate.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from . import config

_lock = threading.RLock()
_controlled = False
_owner_only = True
_deadline = 0.0
_guests: dict[str, str] = {}  # address -> per-grant token; re-grant never resumes old jobs
_revision = 0
_nonce = secrets.token_urlsafe(24)  # old status updates cannot reopen access after a restart
DURATIONS = {0, 15, 60, 240, 1440}  # zero: until the current process ends


def _refresh_locked() -> None:
    global _owner_only, _deadline, _revision, _nonce
    if _controlled and not _owner_only and _deadline and time.time() >= _deadline:
        _owner_only, _deadline = True, 0.0
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1


def snapshot() -> dict[str, Any]:
    with _lock:
        _refresh_locked()
        enabled = _owner_only if _controlled else bool(config.OWNER_ONLY)
        return {"owner_only": enabled, "maintenance": bool(config.MAINTENANCE_MODE),
                "private_mode": enabled or bool(config.MAINTENANCE_MODE),
                "controlled": _controlled, "expires": _deadline, "nonce": _nonce, "revision": _revision,
                "guests": sorted(_guests) if _controlled and not enabled and not config.MAINTENANCE_MODE else []}


def preserve_history() -> bool:
    """Keep legacy data when private at boot or managed at runtime."""
    with _lock:
        return bool(config.OWNER_ONLY or config.MAINTENANCE_MODE or _controlled)


def set_owner_only(enabled: bool, minutes: int = 60, nonce: str = "") -> bool:
    """Override the mode for this process; a new sharing session has no grants."""
    global _controlled, _owner_only, _deadline, _revision, _nonce
    if type(enabled) is not bool or type(minutes) is not int or minutes not in DURATIONS or not config.OWNER_ADDRESS:
        return False
    with _lock:
        _refresh_locked()  # expire and rotate the nonce before validating an opening request
        if not enabled and (config.MAINTENANCE_MODE or not secrets.compare_digest(str(nonce), _nonce)):
            return False
        _controlled = True
        _owner_only = enabled
        _deadline = 0.0 if enabled or not minutes else time.time() + minutes * 60
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1
    return True


def open_one_guest(address: str, minutes: int, nonce: str) -> int | None:
    """Open a session and grant only this address in one atomic transition."""
    global _controlled, _owner_only, _deadline, _revision, _nonce
    address = (address or "").strip().lower()
    if not address or address == config.OWNER_ADDRESS or type(minutes) is not int or minutes not in DURATIONS:
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
    if not address or address == config.OWNER_ADDRESS:
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
        if not _controlled or address == config.OWNER_ADDRESS:
            return False
        _guests.pop(address, None)
        _revision += 1  # also invalidate a still-pending grant of this address
        return True


def service_allowed(address: str) -> bool:
    address = (address or "").strip().lower()
    with _lock:
        _refresh_locked()
        if address and address == config.OWNER_ADDRESS:
            return True
        if config.MAINTENANCE_MODE or (_owner_only if _controlled else config.OWNER_ONLY):
            return False
        return address in _guests if _controlled else True  # legacy deployments still use admin.is_allowed


def revision(address: str) -> int:
    address = (address or "").strip().lower()
    with _lock:
        _refresh_locked()
        if address == config.OWNER_ADDRESS:
            return 0
        # Independent per-grant revisions keep revoking one guest from interrupting others.
        return int(_guests.get(address, "0"), 16) if _controlled else _revision


def session_stamp(address: str) -> str:
    with _lock:
        _refresh_locked()
        address = (address or "").strip().lower()
        return _guests.get(address, "") if _controlled and service_allowed(address) else ""


def bound_allowed(item: dict[str, Any]) -> bool:
    """Never revive a guest's historical jobs or app instances after a new grant."""
    address = str(item.get("addr") or f"chat:{item.get('chat')}").strip().lower()
    with _lock:
        _refresh_locked()
        if not service_allowed(address):
            return False
        if _controlled and address != config.OWNER_ADDRESS:
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
    global _controlled, _owner_only, _deadline, _revision, _nonce
    with _lock:
        _controlled, _owner_only, _deadline = False, True, 0.0
        _guests.clear()
        _nonce = secrets.token_urlsafe(24)
        _revision += 1
