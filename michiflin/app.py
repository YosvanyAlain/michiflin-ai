"""Delta Chat bot wiring: access control, threaded dispatch and startup tasks."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import threading
import time
import urllib.request
from collections import deque
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from deltabot_cli import BotCli
from deltachat2 import (
    EventTypeError,
    EventTypeImapConnected,
    EventTypeIncomingMsg,
    EventTypeSecurejoinInviterProgress,
    EventTypeWebxdcStatusUpdate,
    EventTypeWebxdcRealtimeAdvertisementReceived,
    EventTypeWebxdcRealtimeData,
    EventTypeWarning,
    events,
)

from . import access, admin, agent, ai, browser, chat, coder, commands, config, filekit, filekit_transfer, identity_guard, links, live_web, miniapp, names, perf, profile, progress, queue, rates_monitor, state
from .util import language_signal, tr

log = logging.getLogger("michiflin")
cli = BotCli(config.APP_NAME)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="worker")      # app updates and welcomes
_realtime_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="realtime")
_realtime_slots = threading.BoundedSemaphore(32)
_chat_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat")   # one drainer per busy chat
_chat_locks: dict[int, threading.Lock] = {}
_intake_lock = threading.Lock()
# mdns_enabled is account-wide; serialize any temporary silent mark-seen calls.
_seen_lock = threading.Lock()
_taken: set[int] = set()                   # message ids accepted in this session
_chat_queues: dict[int, deque] = {}
_chat_busy: set[int] = set()
_work_tickets: dict[int, tuple[int, bool]] = {}
_draining = threading.Event()
CATCH_UP_EVERY = 20
MAX_ATTEMPTS = 2
DRAIN_SECONDS = 75
_last_locale: dict[int, str] = {}
_maintenance_notice: dict[str, float] = {}
MAINTENANCE_NOTICE_GAP = 6 * 3600


def _locale(text: str, chat_id: int) -> str:
    """Fixed language, else the command's spelling, else the text, else this chat's last clear language."""
    mode = state.setting("language")
    if mode in {"es", "en"}:
        return mode
    found = names.locale_of(text) or language_signal(text)
    if found:
        _last_locale[chat_id] = found
    return found or _last_locale.get(chat_id, "es")


def _process(msg: Any, text: str, locale: str, address: str) -> None:
    chat_id = msg.chat_id
    lock = _chat_locks.setdefault(chat_id, threading.Lock())
    with _intake_lock:
        ticket = _work_tickets.get(int(msg.id))
    with lock, chat.recipient_guard(chat_id, address, int(getattr(msg, "from_id", 0)), msg):
        chat.set_sender(address)
        queue.set_locale(locale)
        if not chat.recipient_allowed(chat_id):
            return  # queued work must not start after the verified contact changes
        # Safe commands from a revoked/re-granted guest must not replay either.
        if not admin.is_admin(address) and not admin.is_allowed(address):
            return
        if not admin.is_admin(address) and ticket is not None and state.kill_generation(address) != ticket[0]:
            if state.maintenance_interrupted(ticket[0]):
                chat.text(chat_id, miniapp.maintenance_message(locale))
            return
        progress.begin(chat_id, locale, int(msg.id))
        failed = drop = False
        try:
            has_file = bool(getattr(msg, "file", None))
            parsed = commands.parse((text or "").strip()) if not has_file else None
            key = parsed[0] if parsed else ""
            safe = key in {"app", "kill_switch", "nauta_strict", "estado", "health"}
            if admin.is_admin(address) and (key in {"owner_only", "maintenance"} or
                    (key == "admin" and parsed and parsed[1] and names.word_key(parsed[1][0]) in {"allow", "remove", "block"})):
                safe = True  # owner controls stay available in app-only mode and during pauses
            generation, was_killed = ticket or (state.kill_generation(address), state.get_kill_switch(address))
            if not safe and (was_killed or state.work_interrupted(address, generation)):
                chat.text(chat_id, miniapp.maintenance_message(locale) if state.maintenance_interrupted(generation) else miniapp.kill_message(locale))
                return
            # Emergency/status commands remain available even in app-only chat mode.
            if not has_file and state.setting("chat_mode") == "app" and not safe:
                miniapp.point_to_app(chat_id, locale)
                return
            with nullcontext() if safe else state.guard_work(address, generation):
                if has_file and filekit_transfer.is_import_caption(text):
                    try:
                        filekit_transfer.receive(address, msg, text)
                    except filekit.FileKitError as exc:
                        chat.text(chat_id, "⚠️ " + filekit.error_text(str(exc), locale))
                    else:
                        chat.text(chat_id, tr(locale, "✅ Copia importada al kit del servidor temporal. El adjunto original se conservó.",
                                               "✅ Copy imported into the temporary server file kit. The original attachment was kept."))
                    return
                if text.startswith("/") and commands.dispatch(chat_id, text, locale):
                    return
                if has_file:
                    drop = True
                    agent.handle_attachment(chat_id, msg, text, locale)
                    return
                if commands.dispatch(chat_id, text, locale):
                    return
                agent.handle(chat_id, text, locale)
        except state.WorkPaused as exc:
            failed, drop = True, False
            chat.text(chat_id, miniapp.maintenance_message(locale) if exc.maintenance else miniapp.kill_message(locale))
        except Exception as exc:
            failed = True
            log.exception("handler error")
            chat.text(chat_id, tr(locale, f"⚠️ Error interno ({exc.__class__.__name__}). Inténtalo de nuevo.", f"⚠️ Internal error ({exc.__class__.__name__}). Please retry."))
        finally:
            progress.end(failed)
            if drop:
                chat.delete([msg.id])


# ---------------------------------------------------------------------------
# Reliable intake: every incoming message is taken exactly once, even when a live event is missed or the
# session ends mid-way. The core's bot cursor (get_next_msgs / markseen_msgs) plus a small durable inbox in the
# state make unanswered messages resurface in the next session instead of vanishing.
# ---------------------------------------------------------------------------
def _inbox() -> dict[str, int]:
    return state.data.setdefault("inbox", {})


def _screen(bot: Any, accid: int, msg: Any) -> tuple | None:
    """Decide whether a message needs work; raises on transient RPC failures so it is retried later."""
    if msg.from_id in (0, 1, 2) or getattr(msg, "is_info", False):
        return None
    info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
    kind = getattr(info, "chat_type", "")
    if str(getattr(kind, "value", kind)) != "Single":
        return None
    text = (msg.text or "").strip()
    if not text and not getattr(msg, "file", None):
        return None
    address = str(bot.rpc.get_contact(accid, msg.from_id).address or "").lower()
    # A new identity can opt into a fail-closed key pin. The address alone is
    # not proof that this message came from the intended owner.
    if not identity_guard.authorized_chat(bot.rpc, accid, int(msg.chat_id), address,
                                          contact_id=int(msg.from_id), message=msg):
        return None
    grant_stamp = access.session_stamp(address)
    state.act_as(address)
    locale = _locale(text or str(getattr(msg, "file_name", "")), msg.chat_id)
    if not admin.is_allowed(address):
        _refuse(address, text, msg.chat_id, int(msg.from_id))
        return None
    if not config.service_access_allowed(address):
        _maintenance_reply(address, msg.chat_id, locale)
        return None
    admin.seen(address)
    if getattr(info, "is_contact_request", False):
        try:
            bot.rpc.accept_chat(accid, msg.chat_id)
        except Exception:
            if config.EMAIL_KEY_RECOVERY:
                raise  # retry instead of responding to an unaccepted request
        if config.EMAIL_KEY_RECOVERY and not identity_guard.authorized_chat(
                bot.rpc, accid, int(msg.chat_id), address, contact_id=int(msg.from_id)):
            raise RuntimeError("accepted chat is not ready for encrypted delivery")
    # Apply an authenticated emergency ON immediately, rather than queueing it
    # behind a slow request in the same chat. Its confirmation stays in order.
    parsed = commands.parse(text) if not getattr(msg, "file", None) else None
    if parsed and parsed[0] == "maintenance" and config.is_owner_admin(address):
        # Do not queue the emergency control behind another request from this chat.
        chat.set_sender(address)
        with chat.recipient_guard(msg.chat_id, address, int(msg.from_id), msg):
            if chat.recipient_allowed(msg.chat_id):
                commands.cmd_maintenance(msg.chat_id, parsed[1], parsed[2], locale)
        return None
    owner_control = bool(config.is_owner_admin(address) and parsed and (
        parsed[0] in {"app", "kill_switch", "estado", "health", "owner_only"}
        or (parsed[0] == "admin" and parsed[1] and names.word_key(parsed[1][0]) in {"allow", "remove", "block"})))
    if state.maintenance_active() and not owner_control:
        _maintenance_reply(address, msg.chat_id, locale, force=True)
        return None
    if parsed and parsed[0] == "kill_switch" and parsed[2].strip().lower() in commands.YES:
        state.set_kill_switch(address, True)
        queue.wake()
    if parsed and parsed[0] == "owner_only" and admin.is_admin(address):
        values = parsed[1] + parsed[2].split()
        if values and values[0].lower() in commands.YES:
            access.set_owner_only(True)  # close guest access before any slow per-chat work
            queue.wake()
    ticket = (state.kill_generation(address), state.get_kill_switch(address))
    if not admin.is_admin(address) and (not admin.is_allowed(address) or access.session_stamp(address) != grant_stamp):
        return None
    with _intake_lock:
        _work_tickets[int(msg.id)] = ticket
    return msg, text, locale, address


def _accept(bot: Any, accid: int, msg: Any) -> None:
    """Single entry point for incoming messages, from live events and from the catch-up loop."""
    msg_id = int(msg.id)
    with _intake_lock:
        if msg_id in _taken or _draining.is_set():
            return
        _taken.add(msg_id)
    try:
        job = _screen(bot, accid, msg)
    except Exception as exc:
        log.warning("intake of a message postponed: %s", exc.__class__.__name__)
        with _intake_lock:
            _taken.discard(msg_id)
        return
    if job is None:
        _done(msg_id, rpc=bot.rpc, account=accid, message=msg)
        return
    chat_id = int(msg.chat_id)
    with state.lock:
        _inbox().setdefault(str(msg_id), 0)
        ticket = (state.kill_generation(job[3]), state.get_kill_switch(job[3]))
    with _intake_lock:
        _work_tickets.setdefault(msg_id, ticket)
        _chat_queues.setdefault(chat_id, deque()).append(job)
        if chat_id in _chat_busy:
            return
        _chat_busy.add(chat_id)
    _chat_pool.submit(_drain_chat, chat_id)


def _drain_chat(chat_id: int) -> None:
    """Run a chat's messages in order without parking pool threads on a lock."""
    while True:
        with _intake_lock:
            queued = _chat_queues.get(chat_id)
            if not queued or _draining.is_set():
                _chat_busy.discard(chat_id)
                return
            job = queued.popleft()
        msg_id = int(job[0].id)
        with state.lock:
            attempts = _inbox().get(str(msg_id), 0) + 1
            _inbox()[str(msg_id)] = attempts
        state.save()  # persist the work before marking it seen, so a restart can replay it
        try:
            rpc, account = chat.rpc(), chat.account()
            seen = _mark_seen(rpc, account, [msg_id], _receipt_allowed(rpc, account, job[0]))
        except Exception:
            seen = False
        if attempts > MAX_ATTEMPTS:
            log.warning("message skipped after %s interrupted attempts", attempts - 1)
        else:
            try:
                _process(*job)
            except Exception:
                log.exception("message processing failed")
        _done(msg_id, message=job[0], already_seen=seen)


def _receipt_allowed(rpc: Any, account: int, message: Any) -> bool:
    """Never reveal a read receipt to an untrusted sender or group."""
    try:
        if message is None or message.from_id in (0, 1, 2) or getattr(message, "is_info", False):
            return False
        info = rpc.get_basic_chat_info(account, int(message.chat_id))
        kind = getattr(info.chat_type, "value", info.chat_type)
        if str(kind) != "Single" or getattr(info, "is_contact_request", False):
            return False
        address = str(rpc.get_contact(account, int(message.from_id)).address or "").strip().lower()
        return bool(admin.is_allowed(address) and identity_guard.authorized_chat(
            rpc, account, int(message.chat_id), address, contact_id=int(message.from_id), message=message))
    except Exception:
        return False


def _mark_seen(rpc: Any, account: int, ids: list[int], receipt: bool) -> bool:
    """Use Delta Chat's real MDN feature, silently advancing rejected messages.

    MDN configuration is shared by the account. The lock covers both switching
    it off for ignored messages and restoring it, so concurrent workers cannot
    accidentally send a receipt to an unauthorized contact. If switching off
    fails, do not mark that message seen. The core sends a receipt only when
    the sender requested one and the chat permits it.
    """
    with _seen_lock:
        try:
            rpc.set_config(account, "mdns_enabled", "1" if receipt else "0")
            rpc.markseen_msgs(account, ids)
            return True
        except Exception as exc:
            log.warning("markseen postponed: %s", exc.__class__.__name__)
            return False
        finally:
            if not receipt:
                try:
                    rpc.set_config(account, "mdns_enabled", "1")
                except Exception as exc:
                    log.warning("read-receipt setting restore failed: %s", exc.__class__.__name__)


def _done(msg_id: int, *, rpc: Any = None, account: int | None = None,
          message: Any = None, already_seen: bool = False) -> None:
    with _intake_lock:
        _work_tickets.pop(msg_id, None)
    with state.lock:
        completed = _inbox().pop(str(msg_id), None) is not None
        if completed:
            try:
                state.save()
            except (OSError, TypeError, ValueError) as exc:
                log.warning("completed inbox checkpoint postponed: %s", exc.__class__.__name__)
    if not already_seen:
        try:
            rpc = rpc if rpc is not None else chat.rpc()
            account = account if account is not None else chat.account()
            _mark_seen(rpc, account, [msg_id], _receipt_allowed(rpc, account, message))
        except Exception as exc:
            log.debug("markseen postponed: %s", exc.__class__.__name__)


def _catch_up(bot: Any, accid: int) -> None:
    """Pick up messages whose live event never arrived, plus the ones a previous session left unfinished."""
    first = True
    while not _draining.is_set():
        try:
            ids = set(bot.rpc.get_next_msgs(accid))
            if first:
                with state.lock:
                    ids |= {int(key) for key in _inbox()}
            for msg_id in sorted(ids):
                if msg_id in _taken or _draining.is_set():
                    continue
                try:
                    message = bot.rpc.get_message(accid, msg_id)
                except Exception:
                    with state.lock:
                        _inbox().pop(str(msg_id), None)
                    continue
                _accept(bot, accid, message)
        except Exception as exc:
            log.debug("catch-up failed: %s", exc)
        first = False
        _draining.wait(CATCH_UP_EVERY)


def _start_cursor(bot: Any, accid: int) -> None:
    """First run with the cursor: history older than a few minutes counts as handled."""
    try:
        bot.rpc.set_config(accid, "mdns_enabled", "1")
    except Exception as exc:
        log.warning("read-receipt setting postponed: %s", exc.__class__.__name__)
    if state.flag("intake_cursor_v1"):
        return
    try:
        ids = bot.rpc.get_next_msgs(accid)
        old = []
        for msg_id in ids:
            try:
                stamp = float(getattr(bot.rpc.get_message(accid, msg_id), "timestamp", 0) or 0)
            except Exception:
                stamp = 0
            if time.time() - stamp > 600:
                old.append(msg_id)
        if old and not _mark_seen(bot.rpc, accid, old, False):
            return  # do not claim a seeded cursor if silent marking failed
        state.set_flag("intake_cursor_v1", time.time())
        log.info("intake cursor ready: %s old messages marked handled", len(old))
    except Exception as exc:
        log.warning("intake cursor setup failed: %s", exc)


# ---------------------------------------------------------------------------
# Graceful stop: work in progress finishes, outgoing mail leaves, state is saved, then the core stops.
# ---------------------------------------------------------------------------
def _busy() -> int:
    with _intake_lock:
        return len(_chat_busy)


_stopped = threading.Event()


def _graceful(signum: int, _frame: Any) -> None:
    if _stopped.is_set():
        raise KeyboardInterrupt  # drain finished: leave the core loop, which closes the core cleanly
    if _draining.is_set():
        return
    _draining.set()
    miniapp.pause()
    threading.Thread(target=_shutdown, args=(signal.Signals(signum).name,), name="shutdown", daemon=True).start()


def _shutdown(reason: str) -> None:
    log.info("stopping (%s): finishing work in progress", reason)
    live_web.shutdown()
    deadline = time.time() + DRAIN_SECONDS
    while time.time() < deadline and (_busy() or miniapp.busy()):
        time.sleep(1)
    flush_until = time.time() + 20
    while time.time() < flush_until and chat.pending_outgoing():
        time.sleep(1)
    try:
        state.save()
    except Exception as exc:
        log.warning("final state save failed: %s", exc)
    log.info("stopped cleanly: %s chats busy, %s app operations, %s messages unsent", _busy(), miniapp.busy(), chat.pending_outgoing())
    _stopped.set()
    os.kill(os.getpid(), signal.SIGINT)  # wakes the main loop, which then closes the core


def _watch_updates() -> None:
    """A new commit on the deployed branch makes the session hand over gracefully to a fresh one."""
    running, repo, ref = os.environ.get("RUNNING_SHA", ""), os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_REF_NAME", "")
    marker, token = os.environ.get("RESTART_MARKER", ""), os.environ.get("UPDATE_CHECK_TOKEN", "")
    if not (running and repo and ref and marker):
        return
    seen = 0
    while not _draining.is_set():
        _draining.wait(300)
        try:
            request = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{ref}",
                                             headers={"Accept": "application/vnd.github.sha", "User-Agent": "michiflin",
                                                      **({"Authorization": f"Bearer {token}"} if token else {})})
            with urllib.request.urlopen(request, timeout=20) as response:
                latest = response.read(64).decode().strip()
        except Exception as exc:
            log.debug("update check failed: %s", exc)
            continue
        perf.latest_sha = latest or perf.latest_sha
        seen = seen + 1 if latest and latest != running else 0
        if seen >= 2 and not _busy() and not miniapp.busy():
            log.info("new version published: handing over to a fresh session")
            try:
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write(latest)
            except OSError:
                pass
            _graceful(signal.SIGTERM, None)
            return


def _welcome(bot: Any, accid: int, contact_id: int) -> None:
    """Send the app as soon as an authorized contact finishes the secure join."""
    try:
        if not chat.ready():
            chat.bind(bot, accid)
        address = str(bot.rpc.get_contact(accid, contact_id).address or "").lower()
        if not admin.is_allowed(address):
            return
        chat_id = int(bot.rpc.create_chat_by_contact_id(accid, contact_id))
        if not identity_guard.authorized_chat(bot.rpc, accid, chat_id, address, contact_id=contact_id,
                                              allow_rotation=config.EMAIL_KEY_RECOVERY):
            return
        if config.EMAIL_KEY_RECOVERY:
            info = bot.rpc.get_basic_chat_info(accid, chat_id)
            if getattr(info, "is_contact_request", False):
                bot.rpc.accept_chat(accid, chat_id)
            if not identity_guard.authorized_chat(bot.rpc, accid, chat_id, address, contact_id=contact_id):
                return
        lock = _chat_locks.setdefault(chat_id, threading.Lock())
        with lock:
            chat.set_sender(address)
            miniapp.welcome(chat_id, _locale("", chat_id))
    except Exception as exc:
        log.warning("welcome failed: %s", exc)


def _bootstrap_verified_owner(bot: Any, accid: int) -> None:
    """Offer the new miniapp in the already verified owner chat, once bound."""
    if config.EMAIL_KEY_RECOVERY or not (config.BOT_EPOCH_STRICT and config.BOT_VERIFIED_CONTACTS_ONLY and config.OWNER_ONLY):
        return
    try:
        marker = json.loads((config.CONFIG_DIR / ".verified-identity-v1.json").read_text(encoding="utf-8"))
        if (marker.get("confirmed") is not True or marker.get("epoch") != config.BOT_EPOCH
                or str(marker.get("owner_address") or "").casefold() != config.OWNER_ADDRESS.casefold()
                or identity_guard.normalize_fingerprint(marker.get("owner_fingerprint", ""))
                   != identity_guard.normalize_fingerprint(config.OWNER_PGP_FINGERPRINT)):
            return
        chat_id = int(marker["owner_chat"])
        members = bot.rpc.get_chat_contacts(accid, chat_id)
        if len(members) != 1 or not identity_guard.authorized_chat(bot.rpc, accid, chat_id, config.OWNER_ADDRESS,
                                                                    contact_id=int(members[0])):
            return
        lock = _chat_locks.setdefault(chat_id, threading.Lock())
        with lock, chat.recipient_guard(chat_id, config.OWNER_ADDRESS, int(members[0])):
            chat.set_sender(config.OWNER_ADDRESS)
            if chat.recipient_allowed(chat_id) and not miniapp.latest_instance(chat_id):
                miniapp.welcome(chat_id, _locale("", chat_id))
    except Exception as exc:
        log.warning("verified-owner startup deferred (%s)", type(exc).__name__)


def _bootstrap_email_recovery(bot: Any, accid: int) -> None:
    """Restore automatic welcomes for configured admins without reading text.

    In opt-in email mode the newest encrypted incoming message may identify a
    replacement key. This is deliberately vulnerable to address impersonation;
    no wider address list or historical guest grant is activated here.
    """
    if not config.EMAIL_KEY_RECOVERY:
        return
    try:
        addresses = {address.casefold() for address in config.owner_admin_addresses() if admin.is_allowed(address)}
        if not addresses:
            return
        # Only the two explicitly configured admins may be unblocked. No other
        # historical address or individual grant is restored by this step.
        for address in addresses:
            admin._set_blocked_contact(address, False)
        candidates: dict[str, tuple[int, int, int, Any]] = {}
        entries: list[int] = []
        for flags in (0, 1):
            entries.extend(int(x) for x in bot.rpc.get_chatlist_entries(accid, flags, None, None) if int(x) > 9)
        # A contact request or hidden chat may not appear in the normal list.
        # Look up only the explicitly configured administrator addresses.
        priority: list[int] = []
        for address in addresses:
            try:
                for contact in bot.rpc.get_contacts(accid, 0, address):
                    if str(contact.address or "").strip().casefold() != address:
                        continue
                    target = int(bot.rpc.get_chat_id_by_contact_id(accid, int(contact.id)) or 0)
                    if target > 9:
                        priority.append(target)
            except Exception:
                continue
        for chat_id in list(dict.fromkeys(priority + entries))[:500]:
            try:
                info = bot.rpc.get_basic_chat_info(accid, chat_id)
                kind = getattr(info.chat_type, "value", info.chat_type)
                if str(kind) != "Single" or info.is_encrypted is not True or info.is_self_talk is not False:
                    continue
                members = bot.rpc.get_chat_contacts(accid, chat_id)
                if len(members) != 1:
                    continue
                contact_id = int(members[0])
                address = str(bot.rpc.get_contact(accid, contact_id).address or "").strip().casefold()
                if address not in addresses:
                    continue
                bound = identity_guard.current_email_chat(address)
                if bound:
                    if bound == chat_id and getattr(info, "is_contact_request", False):
                        # A crash may have happened after the new key was saved
                        # but before its pending contact request was accepted.
                        contact = bot.rpc.get_contact(accid, contact_id)
                        fingerprint = identity_guard.contact_fingerprint(bot.rpc, accid, contact_id)
                        if (contact.is_key_contact is True and contact.e2ee_avail is True
                                and identity_guard._email_binding(address, chat_id, fingerprint, False)):
                            bot.rpc.accept_chat(accid, chat_id)
                    if bound == chat_id and identity_guard.authorized_chat(bot.rpc, accid, chat_id, address, contact_id=contact_id):
                        candidates[address] = (2**63 - 1, chat_id, contact_id, None)
                    continue
                for msg_id in reversed(bot.rpc.get_message_ids(accid, chat_id, False, False)[-40:]):
                    msg = bot.rpc.get_message(accid, int(msg_id))
                    if int(msg.from_id) != contact_id or msg.show_padlock is not True or getattr(msg, "is_info", False):
                        continue
                    # Inspect without learning or retiring a key: only after
                    # all candidate chats are compared may the newest bind.
                    if not identity_guard.authorized_chat(bot.rpc, accid, chat_id, address,
                                                          contact_id=contact_id, message=msg, _inspect_recovery=True):
                        continue
                    timestamp = int(getattr(msg, "received_timestamp", 0) or 0)
                    old = candidates.get(address)
                    if old is None or (timestamp, int(msg_id)) > (old[0], int(getattr(old[3], "id", 0) or 0)):
                        candidates[address] = (timestamp, chat_id, contact_id, msg)
                    break
            except Exception:
                continue
        for address, (_, chat_id, contact_id, msg) in candidates.items():
            if msg is not None and not identity_guard.authorized_chat(bot.rpc, accid, chat_id, address,
                                                                       contact_id=contact_id, message=msg):
                continue
            info = bot.rpc.get_basic_chat_info(accid, chat_id)
            if getattr(info, "is_contact_request", False):
                bot.rpc.accept_chat(accid, chat_id)
            if not identity_guard.authorized_chat(bot.rpc, accid, chat_id, address, contact_id=contact_id):
                continue
            lock = _chat_locks.setdefault(chat_id, threading.Lock())
            with lock, chat.recipient_guard(chat_id, address, contact_id):
                chat.set_sender(address)
                if chat.recipient_allowed(chat_id):
                    miniapp.welcome(chat_id, _locale("", chat_id))
    except Exception as exc:
        log.warning("email recovery startup deferred (%s)", type(exc).__name__)


_core_seen: dict[str, float] = {}
_CORE_EVENTS = (EventTypeWarning, EventTypeError, EventTypeImapConnected,
                EventTypeIncomingMsg, EventTypeSecurejoinInviterProgress)


@cli.on(events.RawEvent(func=lambda event: isinstance(event, _CORE_EVENTS)))
def on_core_event(bot: Any, accid: int, event: Any) -> None:
    """Surface core connectivity problems and handshakes in the service log."""
    if isinstance(event, EventTypeIncomingMsg):
        log.info("incoming message in chat %s", event.chat_id)
        return
    if isinstance(event, EventTypeSecurejoinInviterProgress):
        log.info("secure join progress %s for contact %s", event.progress, event.contact_id)
        if int(event.progress) == 1000:
            _pool.submit(_welcome, bot, accid, int(event.contact_id))
        return
    # Preserve more diagnostic context, but sanitize before deduplication or logging.
    text = admin.redact_log(str(getattr(event, "msg", "")))[:2000]
    key = text.split(":", 2)[-1][:80]
    now = time.time()
    if now - _core_seen.get(key, 0) < 600:
        return
    _core_seen[key] = now
    level = logging.ERROR if isinstance(event, EventTypeError) else logging.WARNING
    if isinstance(event, EventTypeImapConnected):
        level = logging.INFO
    log.log(level, "core: %s", text)


@cli.on(events.RawEvent(func=lambda event: isinstance(event, EventTypeWebxdcStatusUpdate)))
def on_app_update(bot: Any, accid: int, event: Any) -> None:
    if _draining.is_set():
        return
    if not chat.ready():
        chat.bind(bot, accid)
    _pool.submit(miniapp.on_status_update, int(event.msg_id))


@cli.on(events.RawEvent(func=lambda event: isinstance(event, (EventTypeWebxdcRealtimeAdvertisementReceived, EventTypeWebxdcRealtimeData))))
def on_app_realtime(bot: Any, accid: int, event: Any) -> None:
    if _draining.is_set():
        return
    if not chat.ready():
        chat.bind(bot, accid)
    if not _realtime_slots.acquire(blocking=False):
        return
    def dispatch() -> None:
        try:
            if isinstance(event, EventTypeWebxdcRealtimeAdvertisementReceived):
                live_web.advertisement(int(event.msg_id))
            elif len(event.data) <= live_web.MAX_MESSAGE:
                live_web.receive(int(event.msg_id), event.data)
        finally:
            _realtime_slots.release()
    _realtime_pool.submit(dispatch)


@cli.on(events.NewMessage)
def on_message(bot: Any, accid: int, event: Any) -> None:
    if not chat.ready():
        chat.bind(bot, accid)
    _accept(bot, accid, event.msg)


def _maintenance_reply(address: str, chat_id: int, locale: str, force: bool = False) -> None:
    """Notify an authorized user; operational maintenance answers each attempt."""
    now = time.time()
    if not force and now - _maintenance_notice.get(address, 0.0) < MAINTENANCE_NOTICE_GAP:
        return
    _maintenance_notice[address] = now
    if state.maintenance_active():
        chat.text(chat_id, miniapp.maintenance_message(locale))
    else:
        chat.text(chat_id, tr(locale,
              "🛠️ Michiflin está en mantenimiento y temporalmente solo lo puede usar su dueño.",
              "🛠️ Michiflin is under maintenance and temporarily available only to its owner."))


def _refuse(address: str, text: str, chat_id: int, contact_id: int = 0) -> None:
    """Unknown contacts get no reply: the attempt is logged, admins are alerted and the contact is blocked."""
    log.warning("message from an unauthorized contact in chat %s", chat_id)
    if admin.record_attempt(address, text):
        admin.alert_admins(admin.alert_text(address, text))
    admin.block_unknown(address, contact_id)


def _housekeeping() -> None:
    while True:
        time.sleep(1800)
        try:
            if not access.preserve_history():
                chat.prune_history(3600, keep=queue.source_messages() | miniapp.instance_ids() | {int(key) for key in _inbox()})
            if not access.preserve_history():
                for item in config.TEMP_ROOT.glob("*"):
                    if item.is_file() and time.time() - item.stat().st_mtime > 3600:
                        item.unlink(missing_ok=True)
            state.save()
        except Exception as exc:
            log.warning("housekeeping: %s", exc)


def _background_after_maintenance(function: Any) -> None:
    """Defer startup work while paused; retry safe setup after a pause transition."""
    while not _draining.is_set():
        if state.maintenance_active():
            _draining.wait(1)
            continue
        epoch = state.maintenance_generation()
        function()
        if state.maintenance_generation() == epoch:
            return


@cli.on_start
def on_start(bot: Any, args: Any) -> None:
    accounts = bot.rpc.get_all_account_ids()
    if not accounts:
        return
    accid = args.account or accounts[0]
    chat.bind(bot, accid)
    try:
        if profile.ensure_profile(bot.rpc, accid):
            log.info("bot profile status updated")
    except Exception as exc:
        log.warning("bot profile status could not be updated (%s)", type(exc).__name__)
    links.set_bot_address(chat.self_address())
    state.load()
    # Restore the owner's operational pause after a restart; access gates remain separate.
    from . import maintenance
    notice_state = state.data.get("maintenance_notice") or {}
    if (maintenance.get()["active"] or notice_state.get("profile_suffix")) and not maintenance.sync_profile():
        log.warning("maintenance notice profile could not be updated")
    if not (config.OWNER_ONLY or config.MAINTENANCE_MODE):
        shutil.rmtree(config.TEMP_ROOT, ignore_errors=True)
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    # Never purge pending mail automatically: the owner must explicitly choose
    # whether to remove old messages, especially during a state migration.
    ai.build_registry()
    threading.Thread(target=_background_after_maintenance, args=(ai.discover,), name="discover", daemon=True).start()
    threading.Thread(target=_background_after_maintenance, args=(ai.health_check,), name="ai-health", daemon=True).start()
    threading.Thread(target=lambda: log.info("coder sandbox: %s", "isolated" if coder.isolation() else "off, files only"),
                     name="coder-probe", daemon=True).start()
    if browser.available():
        threading.Thread(target=_background_after_maintenance, args=(browser.warm_up,), name="browser-warmup", daemon=True).start()
    threading.Thread(target=maintenance.scheduler, name="maintenance-scheduler", daemon=True).start()
    threading.Thread(target=rates_monitor.run, args=(_draining,), name="eltoque-monitor", daemon=True).start()
    queue.start()
    _start_cursor(bot, accid)
    _pool.submit(_bootstrap_verified_owner, bot, accid)
    _pool.submit(_bootstrap_email_recovery, bot, accid)
    threading.Thread(target=_catch_up, args=(bot, accid), name="catch-up", daemon=True).start()
    threading.Thread(target=_watch_updates, name="update-watch", daemon=True).start()
    threading.Thread(target=_housekeeping, name="housekeeping", daemon=True).start()
    threading.Thread(target=miniapp.deliver_owner_notice, name="owner-notice", daemon=True).start()
    threading.Thread(target=miniapp.sweep_forever, name="app-sweep", daemon=True).start()
    log.info("Michiflin AI ready: %s providers", len(ai.providers()))


class _Redact(logging.Filter):
    """Server logs may be public: e-mail addresses and invite links never reach them."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = admin.redact_log(record.getMessage()), None
        if record.exc_info:
            import traceback
            record.exc_text = admin.redact_log("".join(traceback.format_exception(*record.exc_info)))
        return True


@cli.on_init
def on_init(bot: Any, args: Any) -> None:
    bot.logger.addFilter(_Redact())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(_Redact())
    admin.install_log_buffer()
    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)  # background jobs start with SIGINT ignored; it must reach us
    try:
        cli.start()
    except KeyboardInterrupt:
        pass
