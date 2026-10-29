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


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # A single public status line, without control characters or markup.
    return " ".join(value.split())[:MAX_TEXT]


def get() -> dict[str, Any]:
    with state.lock:
        value = state.data.get("maintenance_notice")
        value = dict(value) if isinstance(value, dict) else {}
    return {"active": value.get("active") is True, "es": _text(value.get("es")) or DEFAULT_ES,
            "en": _text(value.get("en")) or DEFAULT_EN, "revision": int(value.get("revision") or 0)}


def set_notice(active: bool, es: str, en: str) -> dict[str, Any]:
    if type(active) is not bool or not isinstance(es, str) or not isinstance(en, str):
        raise ValueError("notice")
    if len(es) > MAX_TEXT or len(en) > MAX_TEXT or (active and not _text(es)):
        raise ValueError("notice")
    with state.lock:
        current = state.data.get("maintenance_notice")
        current = dict(current) if isinstance(current, dict) else {}
        starting = active and current.get("active") is not True
        updated = {**current, "active": active, "es": _text(es) or DEFAULT_ES,
                   "en": _text(en) or DEFAULT_EN,
                   "generation": int(current.get("generation") or 0) + int(starting),
                   "revision": int(current.get("revision") or 0) + 1, "updated": time.time()}
        state.data["maintenance_notice"] = updated
        # Save the epoch and active flag atomically before work can be re-admitted.
        state.save()
    from . import queue
    queue.wake()
    if starting:
        # Stop active browser sessions without waiting for a network operation.
        from . import live_web
        live_web.pause_for_maintenance()
    return get()


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
