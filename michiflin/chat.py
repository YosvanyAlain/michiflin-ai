"""Delta Chat messaging helpers shared by handlers and background workers."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deltachat2 import MessageData

from . import config, state

log = logging.getLogger("michiflin")

DELIVERED = {26, 28}
FAILED = 24
PENDING = {18, 20}

_lock = threading.RLock()
_bot: Any = None
_accid: int = 0
_thread = threading.local()
_recent_out: deque[tuple[int, float]] = deque(maxlen=60)
PENDING_OUT = {18, 20}
APP_KEEP = 7 * 86400
WARNING_MARKS = ("⚠️", "❌")


def bind(bot: Any, accid: int) -> None:
    global _bot, _accid
    _bot, _accid = bot, accid


def ready() -> bool:
    return _bot is not None and bool(_accid)


def rpc() -> Any:
    return _bot.rpc


def account() -> int:
    return _accid


@contextmanager
def recipient_guard(chat_id: int, address: str, contact_id: int, message: Any = None):
    """Revalidate a strict chat's recipient before each ordinary outbound handoff."""
    previous = getattr(_thread, "recipient_guard", None)
    if config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY:
        def allowed(target: int) -> bool:
            try:
                from . import admin, identity_guard
                return bool(int(target) == int(chat_id) and ready() and admin.is_allowed(address)
                            and identity_guard.authorized_chat(rpc(), account(), chat_id, address,
                                                               contact_id=contact_id, message=message))
            except Exception:
                return False
        _thread.recipient_guard = allowed
    try:
        yield
    finally:
        _thread.recipient_guard = previous


def recipient_allowed(chat_id: int) -> bool:
    guard = getattr(_thread, "recipient_guard", None)
    return not guard or bool(guard(chat_id))


def send(chat_id: int, text: str = "", file: str | Path | None = None, filename: str = "", html: str = "",
         viewtype: str | None = None, quote: int | None = None, interrupted: Any = None,
         before_handoff: Any = None) -> int:
    state.check_work_allowed()  # don't deliver an interrupted request's result or attachments
    if text and text.lstrip().startswith(WARNING_MARKS):
        _thread.warned = True
    captured = getattr(_thread, "capture", None)
    if captured is not None:
        if file is None:
            captured.append(text)
            return 0
        captured.append({"file": filename or Path(file).name, "text": text})
    data = MessageData(text=text or None, html=html or None, file=str(file) if file else None,
                       filename=filename or None, viewtype=viewtype, quoted_message_id=quote)
    with _lock:
        # A request may have waited for another RPC after its first guard check.
        state.check_work_allowed()
        if not recipient_allowed(chat_id) or (interrupted is not None and interrupted()):
            return 0
        # Persist the delivery intent before the RPC. If the process crashes
        # after this point, do not blindly send the same attachment again.
        if before_handoff is not None and not before_handoff():
            return 0
        try:
            msg_id = int(_bot.rpc.send_msg(_accid, chat_id, data))
        except Exception as exc:
            log.warning("send failed: %s", exc)
            return 0
    _recent_out.append((msg_id, time.time()))
    return msg_id


def pending_outgoing(window: float = 900) -> int:
    """Recently sent messages the core has not handed to the server yet."""
    now, count = time.time(), 0
    for msg_id, sent in list(_recent_out):
        if now - sent > window:
            continue
        try:
            state = getattr(_bot.rpc.get_message(_accid, msg_id), "state", 0)
            count += int(getattr(state, "value", state) or 0) in PENDING_OUT
        except Exception:
            continue
    return count


def text(chat_id: int, message: str, quote: int | None = None) -> int:
    """Send text, splitting very long replies at paragraph boundaries."""
    message = message.strip()
    if not message:
        return 0
    limit = 6000
    if len(message) <= limit:
        return send(chat_id, message, quote=quote)
    first = 0
    while message:
        cut = message.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        sent = send(chat_id, message[:cut].strip(), quote=quote)
        if not first:
            first = sent
        message = message[cut:].strip()
    return first


def start_capture() -> None:
    """Text replies on this thread are collected for the mini app; files still go to the chat."""
    _thread.capture = []


def stop_capture() -> list[Any]:
    collected = getattr(_thread, "capture", None) or []
    _thread.capture = None
    return collected


def capturing() -> bool:
    return getattr(_thread, "capture", None) is not None


def send_update(instance_id: int, payload: Any, notify: dict[str, str] | None = None,
                *, interrupted: Any = None) -> bool:
    """Queue an update only while its current recipient remains authorized."""
    import json

    update: dict[str, Any] = {"payload": payload}
    if notify:
        update["notify"] = notify
    with _lock:
        if interrupted is not None and interrupted():
            return False
        try:
            _bot.rpc.send_webxdc_status_update(_accid, instance_id, json.dumps(update, ensure_ascii=False, separators=(",", ":")), "")
            return True
        except Exception as exc:
            log.warning("webxdc update failed: %s", exc)
            return False


def reset_warned() -> None:
    _thread.warned = False


def set_sender(address: str) -> None:
    _thread.sender = address.lower()
    state.act_as(address)


def sender() -> str:
    """Address of the contact whose message this thread is handling."""
    return str(getattr(_thread, "sender", ""))


def warned() -> bool:
    """True when this thread sent a warning or error reply since reset_warned()."""
    return bool(getattr(_thread, "warned", False))


def react(msg_id: int, emoji: str = "") -> bool:
    """Set (or clear, with an empty emoji) the bot's reaction on a message."""
    if not msg_id or not ready():
        return False
    try:
        with _lock:
            _bot.rpc.send_reaction(_accid, msg_id, [emoji] if emoji else [])
        return True
    except Exception as exc:
        log.debug("reaction failed: %s", exc)
        return False


def message_error(msg_id: int) -> str:
    """Return the core's free-form delivery error for a failed outgoing message, if present."""
    try:
        with _lock:
            message = _bot.rpc.get_message(_accid, msg_id)
        return str(getattr(message, "error", "") or "")[:2000]
    except Exception:
        return ""


def state_of(msg_id: int) -> int:
    try:
        with _lock:
            message = _bot.rpc.get_message(_accid, msg_id)
        value = getattr(message, "state", 0)
        return int(getattr(value, "value", value) or 0)
    except Exception:
        return 0


def wait_delivery(msg_id: int, timeout: int = 1200, stop: threading.Event | None = None) -> str:
    """Block until a sent message is delivered: delivered, failed, missing, timeout or stopped."""
    deadline = time.monotonic() + timeout
    delay = 2.0
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return "stopped"
        current = state_of(msg_id)
        if current in DELIVERED:
            return "delivered"
        if current == FAILED:
            return "failed"
        if current == 0:
            return "missing"
        time.sleep(delay)
        delay = min(delay * 1.4, 15)
    return "timeout"


def delete(message_ids: list[int]) -> None:
    if not message_ids:
        return
    try:
        with _lock:
            _bot.rpc.delete_messages(_accid, message_ids)
    except Exception as exc:
        log.warning("delete failed: %s", exc)


def edit(msg_id: int, new_text: str) -> bool:
    try:
        with _lock:
            _bot.rpc.send_edit_request(_accid, msg_id, new_text)
        return True
    except Exception as exc:
        log.debug("edit failed: %s", exc)
        return False


def delete_for_all(message_ids: list[int]) -> None:
    """Remove messages on the owner's devices too (falls back to a local delete)."""
    if not message_ids:
        return
    try:
        with _lock:
            _bot.rpc.delete_messages_for_all(_accid, message_ids)
    except Exception as exc:
        log.debug("delete for all failed: %s", exc)
        delete(message_ids)


def resend(message_ids: list[int]) -> bool:
    try:
        with _lock:
            _bot.rpc.resend_messages(_accid, message_ids)
        return True
    except Exception:
        return False


def purge_outbox(per_chat: int = 300, chat_id: int | None = None) -> int:
    """Delete unsent outgoing messages; in private mode require the owner's chat scope."""
    if (config.OWNER_ONLY or config.MAINTENANCE_MODE) and chat_id is None:
        return 0
    removed = 0
    try:
        with _lock:
            entries = [chat_id] if chat_id is not None else _bot.rpc.get_chatlist_entries(_accid, None, None, None)
        for target in entries:
            with _lock:
                ids = _bot.rpc.get_message_ids(_accid, target, False, False)[-per_chat:]
            stale = []
            for msg_id in ids:
                try:
                    with _lock:
                        message = _bot.rpc.get_message(_accid, msg_id)
                except Exception:
                    continue
                state = getattr(message, "state", 0)
                state = int(getattr(state, "value", state) or 0)
                if getattr(message, "from_id", 0) == 1 and state in {18, 20, 24}:
                    stale.append(msg_id)
            if stale:
                delete(stale)
                removed += len(stale)
    except Exception as exc:
        log.warning("outbox purge failed: %s", exc)
    return removed


def self_address() -> str:
    try:
        with _lock:
            return str(_bot.rpc.get_config(_accid, "configured_addr") or _bot.rpc.get_config(_accid, "addr") or "")
    except Exception:
        return ""


def prune_history(max_age: int = 3600, per_chat: int = 400, keep: set[int] | None = None) -> int:
    """Delete old local messages (delivered or incoming) so the account database stays small.

    This only affects the bot's own copy; the owner's chat history is untouched.
    """
    removed = 0
    now = time.time()
    try:
        with _lock:
            entries = _bot.rpc.get_chatlist_entries(_accid, None, None, None)
        for chat_id in entries:
            with _lock:
                ids = _bot.rpc.get_message_ids(_accid, chat_id, False, False)[-per_chat:]
            old = []
            for msg_id in ids:
                if keep and msg_id in keep:
                    continue
                try:
                    with _lock:
                        message = _bot.rpc.get_message(_accid, msg_id)
                except Exception:
                    continue
                is_app = str(getattr(getattr(message, "view_type", ""), "value", getattr(message, "view_type", ""))) == "Webxdc"
                if is_app and now - float(getattr(message, "timestamp", now) or now) < APP_KEEP:
                    continue  # app instances must outlive pruning, or their updates can no longer be assigned
                state = getattr(message, "state", 0)
                state = int(getattr(state, "value", state) or 0)
                if now - float(getattr(message, "timestamp", now) or now) < max_age or getattr(message, "is_info", False):
                    continue
                if state in DELIVERED or state < 18:
                    old.append(msg_id)
            if old:
                delete(old)
                removed += len(old)
    except Exception as exc:
        log.warning("history prune failed: %s", exc)
    return removed
