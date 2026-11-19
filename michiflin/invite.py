"""The bot's own invitation link, for the owner to share by hand.

Delta Chat derives this link from the account's own key, so it cannot be
rebuilt from a backup of the configuration alone: it has to be asked of the
running account. The owner needs a way to read it back after losing it, which
is what this module provides.

The link is a credential of sorts -- anyone holding it can start a verified
chat with the bot -- so it is only ever handed to an administrator, and it is
never written to the log or to disk.
"""
from __future__ import annotations

import threading
import time

from . import chat

_CACHE_SECONDS = 300
_lock = threading.Lock()
_cached: tuple[float, str] = (0.0, "")


def available() -> bool:
    """True when the account is connected and can be asked for its link."""
    return chat.ready()


def link(refresh: bool = False) -> str:
    """The account's invitation link, or an empty string when unavailable.

    Cached briefly: the value is stable while the account is, and the owner
    may well open the panel several times in a row looking for the copy
    button. Never raises -- an unreachable account yields an empty string so
    the caller can show a plain explanation instead of a traceback.
    """
    global _cached
    if not available():
        return ""
    with _lock:
        at, value = _cached
        if value and not refresh and time.time() - at < _CACHE_SECONDS:
            return value
    try:
        # chat_id None asks for the account-wide "setup contact" invitation
        # rather than the join link of one particular group.
        value = str(chat.rpc().get_chat_securejoin_qr_code(chat.account(), None) or "").strip()
    except Exception:
        # The account may be offline or mid-restart. Keep any previous value:
        # a slightly old link is still correct, since it is key-derived.
        with _lock:
            return _cached[1]
    if not value:
        return ""
    with _lock:
        _cached = (time.time(), value)
    return value


def forget() -> None:
    """Drop the cached link, e.g. after the account identity changes."""
    global _cached
    with _lock:
        _cached = (0.0, "")
