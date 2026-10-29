"""Owner-managed operational maintenance, separate from the strict access gate.

The message is public profile text: do not put addresses, API keys or other
private information in it. The pause never grants or revokes access;
OWNER_ONLY and MAINTENANCE_MODE remain authoritative.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import chat, state

DEFAULT_ES = "El bot está en mantenimiento. Sus tareas están pausadas y la cola continuará al terminar."
DEFAULT_EN = "The bot is under maintenance. Work is paused and the queue will continue when maintenance ends."
MAX_TEXT = 280
_profile_lock = threading.Lock()
MAX_MINUTES = 525_600  # up to one year; zero is an explicit manual end
_schedule_wake = threading.Event()


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # A single public status line, without control characters or markup.
    return " ".join(value.split())[:MAX_TEXT]


def get() -> dict[str, Any]:
    expire_if_due()
    with state.lock:
        value = state.data.get("maintenance_notice")
        value = dict(value) if isinstance(value, dict) else {}
    return {"active": value.get("active") is True, "es": _text(value.get("es")) or DEFAULT_ES,
            "en": _text(value.get("en")) or DEFAULT_EN, "revision": int(value.get("revision") or 0),
            "expires": float(value.get("expires") or 0)}


def set_notice(active: bool, es: str, en: str, minutes: int | None = None) -> dict[str, Any]:
    if (type(active) is not bool or not isinstance(es, str) or not isinstance(en, str)
            or (minutes is not None and (type(minutes) is not int or not 0 <= minutes <= MAX_MINUTES))):
        raise ValueError("notice")
    if len(es) > MAX_TEXT or len(en) > MAX_TEXT or (active and not _text(es)):
        raise ValueError("notice")
    with state.lock:
        current = state.data.get("maintenance_notice")
        current = dict(current) if isinstance(current, dict) else {}
        starting = active and current.get("active") is not True
        expires = (time.time() + minutes * 60 if minutes else 0.0) if active and minutes is not None else (float(current.get("expires") or 0) if active and not starting else 0.0)
        updated = {**current, "active": active, "es": _text(es) or DEFAULT_ES,
                   "en": _text(en) or DEFAULT_EN, "expires": expires,
                   "generation": int(current.get("generation") or 0) + int(starting),
                   "revision": int(current.get("revision") or 0) + 1, "updated": time.time()}
        state.data["maintenance_notice"] = updated
        # Save the epoch and active flag atomically before work can be re-admitted.
        state.save()
    from . import queue
    queue.wake()
    _schedule_wake.set()
    if starting:
        # Stop active browser sessions without waiting for a network operation.
        from . import live_web
        live_web.pause_for_maintenance()
    return get()


def expire_if_due() -> bool:
    """Atomically end an expired pause; never alter access or manual queue pauses."""
    with state.lock:
        current = state.data.get("maintenance_notice")
        if not isinstance(current, dict) or current.get("active") is not True:
            return False
        try:
            expires = float(current.get("expires") or 0)
        except (TypeError, ValueError):
            return False  # malformed state fails closed
        if not expires or time.time() < expires:
            return False
        current.update(active=False, expires=0.0, revision=int(current.get("revision") or 0) + 1, updated=time.time())
        state.save()
    from . import queue
    queue.wake()

    def announce() -> None:
        from . import miniapp
        notice = get()
        sync_profile()
        miniapp._notify_maintenance(notice)
    threading.Thread(target=announce, name="maintenance-expired", daemon=True).start()
    return True


def scheduler() -> None:
    """Wake near a deadline, including after restart; no automatic activation."""
    while True:
        _schedule_wake.clear()
        expire_if_due()
        with state.lock:
            current = state.data.get("maintenance_notice") or {}
            try:
                expires = float(current.get("expires") or 0) if current.get("active") is True else 0.0
            except (TypeError, ValueError, AttributeError):
                expires = 0.0
        wait = max(0.1, min(30.0, expires - time.time())) if expires else 30.0
        _schedule_wake.wait(wait)


def text(locale: str) -> str:
    notice = get()
    return notice["en" if locale == "en" else "es"] if notice["active"] else ""


def sync_profile() -> bool:
    # A fast on/off must not leave the older public suffix installed last.
    with _profile_lock:
        return _sync_profile()


def _sync_profile() -> bool:
    """Update only our own marked suffix, preserving an externally edited profile.

    The suffix itself is persisted so disabling or editing the notice does not
    erase the user's original profile. Failure is reported rather than claimed.
    """
    if not chat.ready():
        return False
    from . import profile

    try:
        current = str(chat.rpc().get_config(chat.account(), "selfstatus") or "")
        original = current
        with state.lock:
            stored = state.data.get("maintenance_notice") or {}
            previous = str(stored.get("profile_suffix") or "") if isinstance(stored, dict) else ""
        if previous and current.endswith(previous):
            current = current[:-len(previous)].rstrip()
        base = profile.profile_status(current)
        notice = get()
        body = notice["es"] + ("\n" + notice["en"] if notice["en"] != notice["es"] else "")
        suffix = "\n\n🛠️ " + body if notice["active"] else ""
        # Do not truncate existing custom profile text just to fit an advisory.
        if len(base) + len(suffix) > 1400:
            return False
        updated = base + suffix
        if updated != original:
            chat.rpc().set_config(chat.account(), "selfstatus", updated)
        with state.lock:
            record = state.data.setdefault("maintenance_notice", {})
            record["profile_suffix"] = suffix
            state.save()
        return True
    except Exception:
        return False
