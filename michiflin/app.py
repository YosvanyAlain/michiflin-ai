"""Delta Chat bot wiring: access control, threaded dispatch and startup tasks."""
from __future__ import annotations

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

from . import access, admin, agent, ai, browser, chat, coder, commands, config, links, live_web, miniapp, names, perf, profile, progress, queue, state
from .util import language_signal, tr

log = logging.getLogger("michiflin")
cli = BotCli(config.APP_NAME)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="worker")      # app updates and welcomes
_realtime_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="realtime")
_realtime_slots = threading.BoundedSemaphore(32)
_chat_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="chat")   # one drainer per busy chat
_chat_locks: dict[int, threading.Lock] = {}
_intake_lock = threading.Lock()
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
    with lock:
        chat.set_sender(address)
        queue.set_locale(locale)
        # Safe commands from a revoked/re-granted guest must not replay either.
        if not admin.is_admin(address) and (not admin.is_allowed(address)
                or (ticket is not None and state.kill_generation(address) != ticket[0])):
            return
        progress.begin(chat_id, locale, int(msg.id))
        failed = drop = False
        try:
            has_file = bool(getattr(msg, "file", None))
            parsed = commands.parse((text or "").strip()) if not has_file else None
            key = parsed[0] if parsed else ""
            safe = key in {"app", "kill_switch", "nauta_strict", "estado", "health"}
            if admin.is_admin(address) and (key == "owner_only" or
                    (key == "admin" and parsed and parsed[1] and names.word_key(parsed[1][0]) in {"allow", "remove", "block"})):
                safe = True  # owner access controls stay available even in app-only mode or during Kill
            generation, was_killed = ticket or (state.kill_generation(address), state.get_kill_switch(address))
            if not safe and (was_killed or state.work_interrupted(address, generation)):
                chat.text(chat_id, miniapp.kill_message(locale))
                return
            # Emergency/status commands remain available even in app-only chat mode.
            if not has_file and state.setting("chat_mode") == "app" and not safe:
                miniapp.point_to_app(chat_id, locale)
                return
            with nullcontext() if safe else state.guard_work(address, generation):
                if text.startswith("/") and commands.dispatch(chat_id, text, locale):
                    return
                if has_file:
                    drop = True
                    agent.handle_attachment(chat_id, msg, text, locale)
                    return
                if commands.dispatch(chat_id, text, locale):
                    return
                agent.handle(chat_id, text, locale)
        except state.WorkPaused:
            failed, drop = True, False
            chat.text(chat_id, miniapp.kill_message(locale))
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
            pass
    # Apply an authenticated emergency ON immediately, rather than queueing it
    # behind a slow request in the same chat. Its confirmation stays in order.
    parsed = commands.parse(text) if not getattr(msg, "file", None) else None
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
        _done(msg_id)
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
        state.save()
        if attempts > MAX_ATTEMPTS:
            log.warning("message skipped after %s interrupted attempts", attempts - 1)
        else:
            try:
                _process(*job)
            except Exception:
                log.exception("message processing failed")
        _done(msg_id)


def _done(msg_id: int) -> None:
    with _intake_lock:
        _work_tickets.pop(msg_id, None)
    with state.lock:
        _inbox().pop(str(msg_id), None)
    try:
        chat.rpc().markseen_msgs(chat.account(), [msg_id])
    except Exception as exc:
        log.debug("markseen failed: %s", exc)


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
        bot.rpc.set_config(accid, "mdns_enabled", "0")
    except Exception as exc:
        log.debug("mdns setting failed: %s", exc)
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
        if old:
            bot.rpc.markseen_msgs(accid, old)
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
        lock = _chat_locks.setdefault(chat_id, threading.Lock())
        with lock:
            chat.set_sender(address)
            miniapp.welcome(chat_id, _locale("", chat_id))
    except Exception as exc:
        log.warning("welcome failed: %s", exc)


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


def _maintenance_reply(address: str, chat_id: int, locale: str) -> None:
    """Notify an already authorized non-owner sparingly while the service is owner-only."""
    now = time.time()
    if now - _maintenance_notice.get(address, 0.0) < MAINTENANCE_NOTICE_GAP:
        return
    _maintenance_notice[address] = now
    chat.text(chat_id, tr(locale, "🛠️ Michiflin está en mantenimiento y temporalmente solo lo puede usar su dueño.",
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
    if not (config.OWNER_ONLY or config.MAINTENANCE_MODE):
        shutil.rmtree(config.TEMP_ROOT, ignore_errors=True)
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    # Never purge pending mail automatically: the owner must explicitly choose
    # whether to remove old messages, especially during a state migration.
    ai.build_registry()
    threading.Thread(target=ai.discover, name="discover", daemon=True).start()
    threading.Thread(target=ai.health_check, name="ai-health", daemon=True).start()
    threading.Thread(target=lambda: log.info("coder sandbox: %s", "isolated" if coder.isolation() else "off, files only"),
                     name="coder-probe", daemon=True).start()
    if browser.available():
        threading.Thread(target=browser.warm_up, name="browser-warmup", daemon=True).start()
    queue.start()
    _start_cursor(bot, accid)
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
