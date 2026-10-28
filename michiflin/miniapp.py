"""Mini app bridge: the bot ships the webxdc app and answers its requests through status updates.

App -> bot: {"t": "req", "id": str, "op": str, "args": {...}, "lang": "es"|"en"}
Bot -> app: {"t": "res", "re": id, "ok": bool, "data": ..., "error": str}
            {"t": "chunk", "re": id, "i": n, "n": total, "s": str}  (oversized responses, reassembled by the app)
            {"t": "push", "kind": str, "data": ...}                 (queue changes, alerts)
            {"t": "ack", "re": id}                                    (slow request still running)
Requests carry "retry": n when the app resends them; ids are remembered across restarts, so a resend is answered
from the cache (or acknowledged while running) and an old replay is never executed twice.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from . import __version__, access, admin, agent, ai, browser, chat, commands, config, extras, facebook_account, images, links, mail_profiles, media, perf, queue, reader, smart_agent, state, tiktok_account, tools, usage, web, youtube_account
from .util import clean_filename, compact, human_size, tr

log = logging.getLogger("michiflin")
APP_DIR = Path(__file__).resolve().parent.parent / "webxdc"
APP_FILES = ("index.html", "manifest.toml", "icon.png", "app.css", "i18n.js", "core.js", "mp4.js", "brands.js", "app.js", "coder.js")
CHUNK = 90_000
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="app")
# Emergency control must not wait behind long-running app operations.
_control_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-control")
_instance_locks: dict[int, threading.Lock] = {}
_job_marks: dict[str, tuple[str, int]] = {}
Op = Callable[[int, dict[str, Any], str], Any]
OPS: dict[str, Op] = {}


class AppError(Exception):
    """A user-facing failure; the message is already localized."""


def op(name: str) -> Callable[[Op], Op]:
    def register(function: Op) -> Op:
        OPS[name] = function
        return function
    return register


# ---------------------------------------------------------------------------
# Packaging and instances.
# ---------------------------------------------------------------------------
def app_build() -> str:
    """Content hash of the app source and deployment epoch; other epochs are treated as foreign."""
    digest = hashlib.sha256()
    digest.update(f"epoch:{config.BOT_EPOCH}".encode())
    for name in APP_FILES:
        digest.update(name.encode() + (APP_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


def build() -> Path:
    """Zip the app source into a cached .xdc named after its content hash."""
    target = config.TEMP_ROOT / f"michiflin-{app_build()}.xdc"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in APP_FILES:
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED if name.endswith(".png") else zipfile.ZIP_DEFLATED
                archive.writestr(info, (APP_DIR / name).read_bytes())
        partial.replace(target)
    return target


def _apps() -> dict[str, dict[str, Any]]:
    return state.data.setdefault("apps", {})


def send_app(chat_id: int, locale: str, updated: bool = False, address: str = "") -> int:
    if updated:
        text = tr(locale, "✨ Michiflin AI se actualizó. Abre esta app nueva; la anterior se retiró.",
                  "✨ Michiflin AI was updated. Open this new app; the old one was removed.")
    else:
        text = tr(locale, "👋 ¡Hola! Soy Michiflin AI.\n\n"
                          "Todo el bot vive en esta mini app: toca «Iniciar» (o el icono de la app) para abrirla. Dentro tienes la IA, la búsqueda, "
                          "las páginas web, las descargas y los ajustes.\n\n"
                          "No hace falta escribir en este chat: la app habla sola con el bot por correo. "
                          "Las respuestas tardan unos segundos en llegar.",
                  "👋 Hi! I'm Michiflin AI.\n\n"
                  "The whole bot lives in this mini app: tap “Start” (or the app icon) to open it. Inside you'll find the AI, search, "
                  "web pages, downloads and settings.\n\n"
                  "There is no need to type in this chat: the app talks to the bot by email on its own. "
                  "Answers take a few seconds to arrive.")
    msg_id = chat.send(chat_id, text, file=build(), filename="Michiflin AI.xdc", viewtype="Webxdc")
    if msg_id:
        with state.lock:
            recipient = address or chat.sender()
            _apps()[str(msg_id)] = {"chat": chat_id, "serial": 0, "addr": recipient, "sent": time.time(),
                                    "build": app_build(), "epoch": config.BOT_EPOCH,
                                    "access_session": access.session_stamp(recipient)}
            for key in sorted((key for key, entry in _apps().items() if entry["chat"] == chat_id
                               and access.bound_allowed(entry)), key=int)[:-3]:
                _apps().pop(key, None)
        state.save()
    return msg_id


def instance_ids() -> set[int]:
    """App messages that must never be pruned."""
    with state.lock:
        return {int(key) for key in _apps()}


_paused = threading.Event()


def pause() -> None:
    """Stop starting operations; pending requests stay unread for the next session."""
    _paused.set()


def busy() -> int:
    """App operations still running."""
    with _answers_lock:
        return sum(1 for item in _answers.values() if item.get("state") == "running")


_pointed: dict[int, float] = {}


def latest_instance(chat_id: int) -> int:
    """Newest app message of this chat that still exists, or 0."""
    with state.lock:
        keys = sorted((int(key) for key, entry in _apps().items() if int(entry["chat"]) == chat_id
                       and access.bound_allowed(entry)), reverse=True)
    for msg_id in keys:
        try:
            message = chat.rpc().get_message(chat.account(), msg_id)
        except Exception:
            message = None
        if message is not None and int(message.chat_id) == chat_id:
            return msg_id
        with state.lock:
            _apps().pop(str(msg_id), None)
    return 0


def point_to_app(chat_id: int, locale: str) -> None:
    """The chat is only a carrier: answer typed messages by pointing at the app (sending it when missing)."""
    msg_id = latest_instance(chat_id)
    if not msg_id:
        send_app(chat_id, locale)
        return
    if time.time() - _pointed.get(chat_id, 0) < 180:
        return
    _pointed[chat_id] = time.time()
    chat.send(chat_id, tr(locale, "Michiflin funciona dentro de la mini app de este chat: ábrela con «Iniciar» y pide ahí lo que necesites.",
                          "Michiflin works inside the mini app in this chat: open it with “Start” and ask for what you need there."), quote=msg_id)


def welcome(chat_id: int, locale: str) -> None:
    """First contact after the secure join: deliver the app once."""
    with state.lock:
        recent = any(int(entry["chat"]) == chat_id and access.bound_allowed(entry)
                     and time.time() - float(entry.get("sent", 0)) < 600 for entry in _apps().values())
    if not recent:
        send_app(chat_id, locale)


def _instance(msg_id: int) -> dict[str, Any] | None:
    """Known instance registered by this deployment. Foreign epochs are dead; older builds can still hello to upgrade."""
    entry = _apps().get(str(msg_id))
    if entry is None:
        return None
    # Another deployment's packages must not drive this bot at all.
    if entry.get("epoch") and entry.get("epoch") != config.BOT_EPOCH:
        return None
    return entry


SAFE_REPLAY = {"hello", "models", "ask", "image", "search", "read", "tool", "rates", "translate", "health", "repair"}
ACK_AFTER = 12.0        # seconds before a still-running request is acknowledged (one small update)
SWEEP_EVERY = 30.0      # catch updates whose event was missed (restarts, reconnects)
ANSWER_CACHE = 2400.0   # seconds an answer stays ready for a resend
UPGRADE_GAP = 1800.0    # at most one automatic app replacement per chat in this window
_answers: dict[str, dict[str, Any]] = {}
_answers_lock = threading.Lock()
_upgraded: dict[int, float] = {}


def _seen() -> dict[str, list[float]]:
    """Request ids already admitted: id -> [time, retry]. Persisted, so replays after a restart are recognized."""
    return state.data.setdefault("app_seen", {})


def _prune_seen() -> None:
    now = time.time()
    with state.lock:
        seen = _seen()
        if len(seen) > 3000 or any(now - item[0] > 172800 for item in list(seen.values())[:50]):
            keep = sorted(seen.items(), key=lambda item: item[1][0])[-2500:]
            seen.clear()
            seen.update({key: value for key, value in keep if now - value[0] <= 172800})
    with _answers_lock:
        for key in [key for key, item in _answers.items() if now - item["ts"] > ANSWER_CACHE]:
            _answers.pop(key, None)


def _admit(rid: str, retry: int, instance_id: int = 0) -> str:
    """Do not return cached results to a different WebXDC instance, even on ID collision."""
    with state.lock:
        known = _seen().get(rid)
        if known is None:
            _seen()[rid] = [time.time(), retry, instance_id]
            fresh = True
        else:
            fresh = False
            if len(known) > 2 and int(known[2]) != instance_id:
                return "replay"
            if len(known) < 3:
                known.append(instance_id)  # upgrade a legacy seen entry on its first retry
            if retry <= int(known[1]):
                return "replay"
            known[1] = retry
    with _answers_lock:
        item = _answers.get(rid)
        if fresh:
            _answers[rid] = {"state": "running", "ts": time.time(), "body": None, "acked": 0.0}
            return "new"
        if item is None:
            return "lost"
        return "running" if item["state"] == "running" else "cached"


def on_status_update(msg_id: int) -> None:
    """Read new updates of an instance and dispatch the app's requests."""
    if _paused.is_set():
        return
    lock = _instance_locks.setdefault(msg_id, threading.Lock())
    with lock:
        entry = _instance(msg_id)
        if entry is None or not access.bound_allowed(entry):
            return  # old or revoked guest instances cannot replay pending requests
        try:
            raw = chat.rpc().get_webxdc_status_updates(chat.account(), msg_id, int(entry.get("serial", 0)))
            updates = json.loads(raw or "[]")
        except Exception as exc:
            log.warning("webxdc updates unreadable: %s", exc)
            return
        if not updates:
            return
        for update in updates:
            entry["serial"] = max(int(entry.get("serial", 0)), int(update.get("serial", 0)))
            payload = update.get("payload")
            if isinstance(payload, dict) and payload.get("t") == "req":
                entry["active"] = time.time()
                _route(msg_id, dict(entry), payload)
        state.save()


def _route(instance_id: int, entry: dict[str, Any], request: dict[str, Any]) -> None:
    rid = str(request.get("id", ""))[:40]
    if not rid:
        return
    try:
        retry = int(request.get("retry") or 0)
    except (TypeError, ValueError):
        retry = 0
    verdict = _admit(rid, retry, instance_id)
    op = str(request.get("op", ""))
    if verdict == "lost" and op in SAFE_REPLAY:
        with _answers_lock:
            _answers[rid] = {"state": "running", "ts": time.time(), "body": None, "acked": 0.0}
        verdict = "new"
    if verdict == "new":
        address = str(entry.get("addr", "")).strip().lower()
        with state.lock:
            admitted = dict(request, _kill_generation=state.kill_generation(address),
                            _kill_at_admission=state.get_kill_switch(address))
        (_control_pool if op in {"kill_switch", "owner_only"} else _pool).submit(handle, instance_id, entry, admitted)
    elif verdict == "running":
        _ack(instance_id, rid, force=True, entry=entry)
    elif verdict == "cached":
        with _answers_lock:
            body = (_answers.get(rid) or {}).get("body")
        if body and access.bound_allowed(entry):
            log.info("app resend %s answered from cache", op)
            _deliver(instance_id, rid, body)
    elif verdict == "lost":
        locale = request.get("lang") if request.get("lang") in {"es", "en"} else "es"
        respond(instance_id, rid, None, tr(locale, "La respuesta se perdió durante un reinicio del bot. Revisa el resultado (por ejemplo, en Descargas) y repítelo si hace falta.",
                                           "The answer was lost while the bot restarted. Check the result (for example, in Downloads) and repeat it if needed."))


def _ack(instance_id: int, rid: str, force: bool = False, entry: dict[str, Any] | None = None) -> None:
    if entry is not None and not access.bound_allowed(entry):
        return
    with _answers_lock:
        item = _answers.get(rid)
        if not item or item["state"] != "running" or (not force and item["acked"]) or time.time() - item["acked"] < 20:
            return
        item["acked"] = time.time()
    chat.send_update(instance_id, {"t": "ack", "re": rid})


WORK_OPS = {"ask", "image", "code", "read", "search", "action", "command", "tool", "translate", "rates",
            "youtube_account", "facebook_account", "tiktok_account"}
AI_OPS = {"ask", "image", "code"}
KILL_SAFE_OPS = {"hello", "health", "kill_switch", "owner_only", "nauta_strict", "settings", "models", "estado"}
OP_LIMITS = {"ask": 240, "image": 480, "read": 150, "action": 150, "search": 100, "translate": 90, "code": 900}
_workers = ThreadPoolExecutor(max_workers=16, thread_name_prefix="app-op")
_control_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-control-op")
_ai_gates: dict[str, threading.Lock] = {}


def _ai_gate(address: str) -> threading.Lock | None:
    """People other than administrators run one AI request at a time."""
    if not address or admin.is_admin(address):
        return None
    with _answers_lock:
        return _ai_gates.setdefault(address, threading.Lock())


def kill_message(locale: str) -> str:
    return tr(locale, "🛑 Operación pausada por el kill switch. Al desactivarlo, revisa Descargas y repite solo si hace falta.",
              "🛑 Operation paused by the kill switch. After turning it off, check Downloads and retry only if needed.")


def handle(instance_id: int, entry: dict[str, Any], request: dict[str, Any]) -> None:
    rid = str(request.get("id", ""))[:40]
    locale = request.get("lang") if request.get("lang") in {"es", "en"} else "es"
    op = str(request.get("op", ""))
    function = OPS.get(op)
    chat_id = int(entry["chat"])
    address = str(entry.get("addr", "")).strip().lower()

    def reply(data: Any, error: str = "") -> None:
        if access.bound_allowed(entry) and admin.is_allowed(address):
            respond(instance_id, rid, data, error)

    if not access.bound_allowed(entry):
        return  # never replay commands from, or respond to, a stale guest instance
    if not address or not admin.is_allowed(address):
        reply(None, tr(locale, "Acceso revocado. Solicita una nueva autorización.",
                                           "Access revoked. Please request authorization again."))
        return
    if not config.service_access_allowed(address):
        reply(None, tr(locale, "Michiflin está en mantenimiento y solo lo puede usar su dueño.",
                                           "Michiflin is under maintenance and available only to its owner."))
        return
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    # Local authorization status and disconnection remain available while work
    # is paused; starting, polling or reading providers is kill-switch guarded.
    social_ops = {"youtube_account", "facebook_account", "tiktok_account"}
    kill_safe = op in KILL_SAFE_OPS or (op in social_ops and args.get("do", "status") in {"status", "disconnect", "cancel"})
    generation = int(request.get("_kill_generation", state.kill_generation(address)))
    was_killed = bool(request.get("_kill_at_admission", state.get_kill_switch(address)))
    if not kill_safe and (was_killed or state.work_interrupted(address, generation)):
        reply(None, kill_message(locale))
        return
    # Older packages may only run hello so the bot can replace them; other ops stay blocked.
    if entry.get("build") and entry.get("build") != app_build() and op not in {"hello", "health"}:
        reply(None, tr(locale, "Esta mini app ya no es válida. Abre la versión nueva del chat.",
                                           "This mini app is no longer valid. Open the new version from the chat."))
        return
    admin.seen(address)
    if function is None:
        reply(None, tr(locale, "Operación desconocida.", "Unknown operation."))
        return

    def run() -> Any:
        # Re-check when a worker actually starts: revocation can happen after
        # an app request was admitted but before it leaves the executor queue.
        if not access.bound_allowed(entry) or not admin.is_allowed(address):
            raise AppError(tr(locale, "Acceso revocado.", "Access revoked."))
        if not config.service_access_allowed(address):
            raise AppError(tr(locale, "Michiflin está en mantenimiento.", "Michiflin is under maintenance."))
        chat.set_sender(address)
        gate = _ai_gate(address) if op in AI_OPS else None
        if gate is not None and not gate.acquire(blocking=False):
            raise AppError(tr(locale, "Espera a que termine tu consulta anterior.", "Wait until your previous request finishes."))
        try:
            if kill_safe:
                answer = function(chat_id, args, locale)
            else:
                with state.guard_work(address, generation):
                    answer = function(chat_id, args, locale)
            if op in AI_OPS and isinstance(answer, dict):
                answer["quota"] = usage.quota_view(address)
            return answer
        finally:
            if gate is not None:
                gate.release()
            chat.stop_capture()

    started = time.monotonic()
    future = (_control_workers if op in {"kill_switch", "owner_only"} else _workers).submit(run)
    timer = threading.Timer(ACK_AFTER, _ack, args=(instance_id, rid), kwargs={"entry": entry})
    timer.daemon = True
    timer.start()
    outcome = "ok"
    try:
        reply(future.result(timeout=OP_LIMITS.get(op, 120)))
    except FutureTimeout:
        outcome = "timeout"
        reply(None, tr(locale, "La respuesta tardó demasiado; la operación podría seguir en curso. Revisa Descargas antes de repetir.",
                                           "The response took too long; the operation may still be running. Check Downloads before retrying."))
    except state.WorkPaused:
        outcome = "refused: kill switch"
        reply(None, kill_message(locale))
    except AppError as exc:
        outcome = f"refused: {compact(str(exc), 120)}"
        reply(None, str(exc))
    except Exception as exc:
        outcome = f"error: {exc.__class__.__name__}"
        log.exception("mini app operation %s failed", op)
        reply(None, tr(locale, f"Error interno ({exc.__class__.__name__}).", f"Internal error ({exc.__class__.__name__})."))
    finally:
        timer.cancel()
        seconds = time.monotonic() - started
        level = logging.WARNING if outcome == "timeout" or outcome.startswith("error") else logging.INFO
        log.log(level, "app %s %s in %.1fs", op, outcome, seconds)
        perf.note_op(op, seconds, outcome == "ok" or outcome.startswith("refused"))
        if op in WORK_OPS:
            usage.note(address, requests=1)
    if op == "hello" and access.bound_allowed(entry) and admin.is_allowed(address):
        _maybe_upgrade(instance_id, entry, locale)


def _maybe_upgrade(instance_id: int, entry: dict[str, Any], locale: str) -> None:
    """An instance sent with older app code is replaced by a fresh one (and removed), once per window."""
    if entry.get("build") == app_build():
        return
    chat_id = int(entry["chat"])
    if time.time() - _upgraded.get(chat_id, 0.0) < UPGRADE_GAP:
        return
    _upgraded[chat_id] = time.time()
    replace_app(instance_id, chat_id, locale, str(entry.get("addr", "")))


def replace_app(instance_id: int, chat_id: int, locale: str, address: str = "") -> int:
    new_id = send_app(chat_id, locale, updated=True, address=address)
    if new_id:
        log.info("app instance %s replaced by %s", instance_id, new_id)
        chat.delete_for_all([instance_id])
        with state.lock:
            _apps().pop(str(instance_id), None)
        state.save()
    return new_id


def respond(instance_id: int, rid: str, data: Any, error: str = "") -> None:
    body = {"t": "res", "re": rid, "ok": not error, "data": data, "error": error}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    with _answers_lock:
        item = _answers.setdefault(rid, {"ts": time.time(), "acked": 0.0})
        item.update(state="done", ts=time.time(), body=text if len(text) <= 600_000 else None)
    _deliver(instance_id, rid, text)


def _deliver(instance_id: int, rid: str, text: str) -> None:
    if len(text.encode("utf-8")) <= CHUNK:
        chat.send_update(instance_id, json.loads(text))
        return
    pieces = [text[start:start + CHUNK // 2] for start in range(0, len(text), CHUNK // 2)]
    for index, piece in enumerate(pieces):
        chat.send_update(instance_id, {"t": "chunk", "re": rid, "i": index, "n": len(pieces), "s": piece})


def sweep_forever() -> None:
    """Re-read recently active instances: an update whose event was missed is still answered within seconds."""
    first = True
    while True:
        try:
            now = time.time()
            with state.lock:
                keys = [int(key) for key, entry in _apps().items()
                        if first or now - float(entry.get("active", 0)) < 86400 or now - float(entry.get("sent", 0)) < 172800]
            for msg_id in keys:
                lock = _instance_locks.setdefault(msg_id, threading.Lock())
                if not lock.locked():
                    on_status_update(msg_id)
            _prune_seen()
        except Exception as exc:
            log.debug("app sweep failed: %s", exc)
        first = False
        time.sleep(SWEEP_EVERY)


def push(chat_id: int, kind: str, data: Any, notify: str = "") -> None:
    """Send a push to the newest app instance of a chat (with an optional system notification)."""
    with state.lock:
        found = [(int(key), entry) for key, entry in _apps().items() if int(entry["chat"]) == chat_id
                 and access.bound_allowed(entry)]
    if not found or not chat.ready():
        return
    instance_id, entry = max(found, key=lambda item: item[0])
    address = str(entry.get("addr", ""))
    if not access.bound_allowed(entry):
        return
    chat.send_update(instance_id, {"t": "push", "kind": kind, "data": data}, {address: notify[:120]} if notify and address else None)


def _on_job(job: dict[str, Any], values: dict[str, Any]) -> None:
    """Push queue changes on status transitions, delivered parts and the halfway mark only (each push is an e-mail)."""
    total = int(job.get("size") or job.get("size_est") or 0)
    half = 1 if job["status"] == "downloading" and total and int(job.get("progress") or 0) * 2 >= total else 0
    mark = (f"{job['status']}:{int(bool(job.get('hold')))}:{job.get('sent', 0)}:{len(job.get('todo') or [])}", half)
    if _job_marks.get(job["id"]) == mark:
        return
    _job_marks[job["id"]] = mark
    locale = job.get("locale", "es")
    notify = ""
    if job["status"] == "done" and "status" in values:
        notify = tr(locale, f"Listo: {job.get('name') or job['title']}", f"Done: {job.get('name') or job['title']}")
    elif job["status"] == "failed" and "status" in values:
        notify = tr(locale, f"Falló: {job.get('name') or job['title']}", f"Failed: {job.get('name') or job['title']}")
    push(int(job["chat"]), "job", job_view(job, locale), notify)


queue.LISTENERS.append(_on_job)


def _on_health(kind: str, spanish: str, english: str) -> None:
    """Model health changes reach the owner's app as a notice with a system notification."""
    with state.lock:
        chats = {int(entry["chat"]) for entry in _apps().values() if str(entry.get("addr", "")).lower() == config.OWNER_ADDRESS}
    for chat_id in chats:
        push(chat_id, "notice", {"kind": kind, "es": spanish, "en": english}, tr(state.setting("language") if state.setting("language") != "auto" else "es",
                                                                                   spanish, english))


ai.LISTENERS.append(_on_health)


def deliver_owner_notice(wait: float = 600, step: float = 10) -> bool:
    """Show the deployer's OWNER_NOTICE once in the owner's app; retried until the chat is ready."""
    text = config.OWNER_NOTICE.strip()
    if not text:
        return False
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    if state.flag("owner_notice") == digest:
        return False
    deadline = time.time() + wait
    while True:
        with state.lock:
            chats = {int(entry["chat"]) for entry in _apps().values() if str(entry.get("addr", "")).lower() == config.OWNER_ADDRESS}
        if chats and chat.ready():
            for chat_id in chats:
                push(chat_id, "notice", {"kind": "info", "es": text, "en": text}, text)
            state.set_flag("owner_notice", digest)
            log.info("owner notice delivered")
            return True
        if time.time() >= deadline:
            log.warning("owner notice not delivered: no app chat for the owner yet")
            return False
        time.sleep(step)


# ---------------------------------------------------------------------------
# Shared views.
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    return str(text or "").strip()


def actions_in(text: str, locale: str) -> list[dict[str, Any]]:
    """Tap commands found in a bot reply, with their canonical key so the app can label them."""
    found = []
    for token in dict.fromkeys(re.findall(r"(?<![\w/])/[a-zA-Z][\w.@-]*", text)):
        parsed = commands.parse(token)
        if parsed and parsed[0]:
            found.append({"t": token, "k": parsed[0], "a": parsed[1]})
    return found


def captured_reply(collected: list[Any], locale: str) -> dict[str, Any]:
    texts = [_clean(item) for item in collected if isinstance(item, str) and _clean(item)]
    files = [item["file"] for item in collected if isinstance(item, dict)]
    text = "\n\n".join(texts)
    if files and not text:
        text = tr(locale, "Enviado al chat.", "Sent to the chat.")
    return {"text": text, "actions": actions_in(text, locale), "files": files}


def run_captured(function: Callable[[], Any], locale: str) -> dict[str, Any]:
    chat.start_capture()
    try:
        function()
    finally:
        collected = chat.stop_capture()
    return captured_reply(collected, locale)


def job_view(job: dict[str, Any], locale: str) -> dict[str, Any]:
    parts = int(job.get("parts") or 0)
    delivered = parts - len(job.get("todo") or []) if job["status"] in {"sending", "done", "ready"} else int(job.get("sent") or 0)
    return {
        "id": job["id"], "title": job.get("name") or job["title"], "kind": job["kind"], "status": job["status"],
        "hold": bool(job.get("hold")), "size": int(job.get("size") or job.get("size_est") or 0), "done": int(job.get("progress") or 0),
        "parts": parts, "delivered": max(0, delivered), "format": job.get("part_format", ""),
        "error": queue._error_text(job["error"], locale) if job["status"] == "failed" and job.get("error") else "",
        "created": float(job.get("created") or 0), "updated": float(job.get("updated") or 0),
        "sha": job.get("sha", ""), "part_sha": job.get("part_sha", []), "part_sizes": job.get("part_sizes", []),
    }


def jobs_view(chat_id: int, locale: str) -> list[dict[str, Any]]:
    with state.lock:
        mine = sorted((job for job in queue.jobs().values() if int(job["chat"]) == chat_id
                       and access.job_allowed(job)), key=lambda item: int(item["id"]))
    return [job_view(job, locale) for job in mine[-25:]]


def _next_reset() -> float:
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc).timestamp()


def models_view() -> dict[str, Any]:
    providers = []
    for item in ai.providers():
        models = []
        for model in item.models:
            remaining = state.remaining_for(model.key)
            models.append({"key": model.key, "title": model.title(), "roles": sorted(model.roles), "used": state.usage_today(model.key),
                           "daily": model.daily, "remaining": list(remaining) if remaining else None,
                           "cooldown": state.cooldown_until(model.key), "reason": state.cooldown_reason(model.key),
                           "health": ai.model_status(model.key), "id": model.model, "accepts": ai.accepts(model), "manual": model.weight <= 0})
        providers.append({"key": item.key, "name": item.name, "daily": item.daily or sum(model.daily for model in item.models),
                          "used": sum(state.usage_today(model.key) for model in item.models),
                          "cooldown": state.cooldown_until(item.key), "reason": state.cooldown_reason(item.key), "models": models})
    sources = [{"key": source.key, "label": source.label, "daily": source.daily, "used": state.usage_today(source.key),
                "cooldown": state.cooldown_until(source.key), "reason": state.cooldown_reason(source.key)} for source in images.sources()]
    return {"now": time.time(), "reset": _next_reset(), "providers": providers, "images": sources, "pin": state.setting("ai_pin")}


def health_view() -> dict[str, Any]:
    elapsed = time.monotonic() - config.STARTED_AT
    remaining = max(0.0, config.SESSION_SECONDS - elapsed)
    disk = shutil.disk_usage(config.JOB_ROOT if config.JOB_ROOT.exists() else "/")
    return {
        "version": __version__, "now": time.time(), "uptime": elapsed, "restart": time.time() + remaining,
        "active": queue.active_count(), "queue_bytes": queue.queue_bytes(), "disk_free": disk.free,
        "sent": usage.sent_today(chat.sender()) if chat.sender() else state.sent_last_day(), "cap_mb": int(state.setting("daily_cap_mb")),
        "search": {"you": bool(config.YDC_API_KEY), "used": state.usage_today("you:search"), "daily": config.YDC_DAILY},
        "tools": {"ffmpeg": bool(shutil.which("ffmpeg")), "7z": bool(shutil.which("7z") or shutil.which("7zz")),
                  "browser": browser.available(), "media": media.available()},
    }


# Settings shown in the app: key -> (Spanish label, English label, kind, choices as (value, Spanish, English)).
LEVELS = (("low", "Baja", "Low"), ("medium", "Media", "Medium"), ("high", "Alta", "High"))
YES_NO = (("yes", "Sí", "Yes"), ("no", "No", "No"))
APP_SETTINGS: list[tuple[tuple[str, str], list[tuple[str, str, str, str, tuple]]]] = [
    (("Descargas", "Downloads"), [
        ("video_height", "Calidad de video", "Video quality", "choice", tuple((str(v), f"{v}p", f"{v}p") for v in (144, 240, 360, 480, 720, 1080))),
        ("audio_kbps", "Calidad de audio", "Audio quality", "choice", tuple((str(v), f"{v} kbps", f"{v} kbps") for v in (32, 48, 64, 96, 128, 160))),
        ("audio_format", "Formato de audio", "Audio format", "choice", (("m4a", "M4A", "M4A"), ("mp3", "MP3", "MP3"), ("opus", "Opus", "Opus"))),
        ("part_mb", "Tamaño de cada parte (MB)", "Part size (MB)", "number", (1, 20)),
        ("part_format", "Partes de archivos", "File parts", "choice", (("raw", "Partes simples", "Plain parts"), ("7z", "7z", "7z"), ("zip", "ZIP", "ZIP"))),
    ]),
    (("Envío", "Delivery"), [
        ("auto_send", "Enviar al terminar", "Send when ready", "choice", YES_NO),
        ("batch", "Partes por tanda (0 = todas)", "Parts per batch (0 = all)", "number", (0, 200)),
        ("delay", "Pausa entre partes (s)", "Pause between parts (s)", "number", (0, 3600)),
        ("window", "Horario de envío (2200-0600)", "Delivery window (2200-0600)", "text", ()),
        ("daily_cap_mb", "Tope diario (MB, 0 = sin tope)", "Daily cap (MB, 0 = none)", "number", (0, 100000)),
    ]),
    (("Web", "Web"), [
        ("reader_kb", "Tamaño de página (KB)", "Page size (KB)", "number", (30, 900)),
        ("reader_images", "Imágenes en páginas", "Images in pages", "choice", (("off", "No", "No"), ("low", "Pequeñas", "Small"))),
        ("capture_device", "Capturas como", "Screenshots as", "choice", (("mobile", "Móvil", "Mobile"), ("desktop", "PC", "Desktop"))),
        ("capture_quality", "Nitidez de capturas", "Screenshot sharpness", "choice", (("normal", "Normal", "Normal"), ("high", "Alta", "High"), ("max", "Máxima", "Max"))),
    ]),
    (("Bot", "Bot"), [
        ("language", "Idioma en el chat", "Chat language", "choice", (("auto", "Automático", "Automatic"), ("es", "Español", "Spanish"), ("en", "Inglés", "English"))),
        ("notify", "Mensajes en el chat", "Chat messages", "choice", (("minimal", "Mínimos", "Minimal"), ("normal", "Normales", "Normal"))),
        ("progress", "Avisos de progreso", "Progress notices", "choice", (("on", "Sí", "Yes"), ("off", "No", "No"))),
        ("reactions", "Reacciones", "Reactions", "choice", (("on", "Sí", "Yes"), ("off", "No", "No"), ("all", "Todas", "All"))),
        ("chat_mode", "Uso del chat", "Chat use", "choice", (("app", "Solo la app", "App only"), ("full", "App y comandos", "App and commands"))),
        ("image_size", "Tamaño de imágenes IA", "AI image size", "choice", (("512", "512", "512"), ("768", "768", "768"), ("1024", "1024", "1024"))),
    ]),
    (("Para todos", "For everyone"), [
        ("user_jobs", "Descargas a la vez por persona", "Downloads at once per person", "number", (1, 20)),
        ("user_daily_mb", "Cuota diaria por persona (MB, 0 = sin cuota)", "Daily quota per person (MB, 0 = none)", "number", (0, 100000)),
        ("ai_daily", "Consultas de IA al día por persona (0 = sin límite)", "AI requests a day per person (0 = no limit)", "number", (0, 1000)),
        ("image_daily", "Imágenes al día por persona (0 = sin límite)", "Images a day per person (0 = no limit)", "number", (0, 200)),
        ("code_daily", "Proyectos del Programador al día por persona (0 = sin límite)", "Coder projects a day per person (0 = no limit)", "number", (0, 50)),
        ("ai_reserve", "IA reservada a la administración (%)", "AI kept for administration (%)", "number", (0, 90)),
        ("ttl_hours", "Conservar archivos (horas)", "Keep files (hours)", "number", (1, 168)),
    ]),
]


def _setting_value(key: str) -> str:
    value = state.setting(key)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value if value is not None else "")


def settings_view(locale: str) -> list[dict[str, Any]]:
    """Personal settings for everyone; administrators also get the shared group."""
    en = locale == "en"
    private = access.snapshot()["private_mode"]
    state.own_settings()
    groups = []
    for (title_es, title_en), items in APP_SETTINGS:
        rows = []
        for key, label_es, label_en, kind, extra in items:
            if private and key in {"user_jobs", "user_daily_mb", "ai_daily", "image_daily", "code_daily", "ai_reserve"}:
                continue  # legacy sharing controls do not apply to the private owner
            if not state.can_change(key):
                continue
            row: dict[str, Any] = {"key": key, "label": label_en if en else label_es, "kind": kind, "value": _setting_value(key)}
            if key == "part_mb":
                row["value"] = str(mail_profiles.effective_settings(chat.sender(), {"part_mb": int(state.setting("part_mb"))})["part_mb"])
            if kind == "choice":
                row["choices"] = [{"v": value, "l": english if en else spanish} for value, spanish, english in extra]
            elif kind == "number":
                row["min"], row["max"] = extra
            rows.append(row)
        if rows:
            title = tr(locale, "General", "General") if private and title_en == "For everyone" else (title_en if en else title_es)
            groups.append({"title": title, "items": rows, "shared": any(row["key"] not in state.PERSONAL for row in rows)})
    return groups


# ---------------------------------------------------------------------------
# Operations.
# ---------------------------------------------------------------------------
@op("hello")
def op_hello(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return {"version": __version__, "admin": admin.is_admin(chat.sender()), "models": models_view(), "health": health_view(),
            "settings": settings_view(locale), "jobs": jobs_view(chat_id, locale), "quota": usage.quota_view(chat.sender())}


@op("repair")
def op_repair(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Fresh snapshot for an app that cleared its stuck requests."""
    return op_hello(chat_id, args, locale)


@op("reinstall")
def op_reinstall(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Send a fresh app and remove every older instance of this chat (what deleting the chat used to fix)."""
    with state.lock:
        old = [(int(key), str(entry.get("addr", ""))) for key, entry in _apps().items() if int(entry["chat"]) == chat_id
               and access.bound_allowed(entry)]
    new_id = send_app(chat_id, locale, updated=True, address=old[0][1] if old else "")
    if not new_id:
        raise AppError(tr(locale, "No pude enviar la app nueva. Inténtalo otra vez.", "Could not send the new app. Please try again."))
    _upgraded[chat_id] = time.time()
    ids = [key for key, _address in old if key != new_id]
    chat.delete_for_all(ids)
    with state.lock:
        for key in ids:
            _apps().pop(str(key), None)
    state.save()
    return {"sent": True}


@op("models")
def op_models(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return models_view()


@op("ask")
def op_ask(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    text = _clean(args.get("text"))[:6000]
    space = str(args.get("space") or "agent")
    attachment = args.get("file") if isinstance(args.get("file"), dict) and space not in {"agent", "smart_agent"} else None
    if not text and not attachment:
        raise AppError(tr(locale, "Escribe algo primero.", "Write something first."))
    media_parts: list[tuple[str, bytes]] = []
    if attachment:
        text, media_parts = _attachment(attachment, text, space, locale)
    if space == "agent":
        return run_captured(lambda: agent.handle(chat_id, text, locale), locale)
    history = args.get("history") if isinstance(args.get("history"), list) else []
    if space == "smart_agent":
        try:
            answer = smart_agent.run(text, locale, history)
        except smart_agent.AgentError as exc:
            raise AppError(str(exc)) from exc
        answer["models"] = models_view()
        return answer
    messages = [{"role": "system", "content": agent.system_prompt(locale)}]
    budget = 8000
    for turn in reversed(history[-12:]):
        if isinstance(turn, list) and len(turn) == 2 and turn[0] in {"user", "assistant"}:
            content = _clean(turn[1])[:2000]
            budget -= len(content)
            if budget < 0:
                break
            messages.insert(1, {"role": turn[0], "content": content})
    messages.append({"role": "user", "content": text})
    if space == "auto":
        result = ai.complete(messages, agent.pick_role(text), images=media_parts or None)
    else:
        result = ai.complete_exact(messages, space, images=media_parts or None)
    if not result.ok:
        raise AppError(ai.describe_error(result.error, locale, result, "" if space == "auto" else space).replace("⚠️ IA: ", "").replace("⚠️ ", ""))
    return {"text": result.text, "sig": result.signature(), "model": result.model, "models": models_view()}


@op("youtube_account")
def op_youtube_account(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Read-only, per-sender Google device authorization; no tokens enter the app."""
    address, action = chat.sender(), str(args.get("do") or "status")
    try:
        if action == "status":
            return youtube_account.status(address)
        if action == "start":
            return youtube_account.start(address)
        if action == "poll":
            return youtube_account.poll(address)
        if action in {"channels", "subscriptions"}:
            return youtube_account.read(address, action)
        if action == "disconnect":
            return youtube_account.disconnect(address)
    except youtube_account.AccountError as exc:
        messages = {
            "not_configured": ("Falta configurar la conexión de YouTube en el servidor.", "The YouTube connection is not configured on the server."),
            "not_connected": ("Todavía no has conectado YouTube.", "You have not connected YouTube yet."),
            "network": ("No pude contactar con Google. Inténtalo de nuevo.", "Could not reach Google. Please retry."),
            "reauthorize": ("La autorización caducó o fue revocada. Conecta tu cuenta de nuevo.", "Authorization expired or was revoked. Connect your account again."),
            "vault": ("No se pudo abrir la sesión cifrada. Comprueba la clave del servidor.", "Could not open the encrypted session. Check the server key."),
            "scope": ("Google no concedió el permiso de solo lectura requerido.", "Google did not grant the required read-only scope."),
        }
        spanish, english = messages.get(str(exc), ("Google no completó la operación. Revisa la configuración e inténtalo otra vez.",
                                                       "Google did not complete the operation. Check the configuration and retry."))
        raise AppError(tr(locale, spanish, english)) from None
    raise AppError(tr(locale, "Operación desconocida.", "Unknown operation."))


def _social_account(provider: Any, service: str, args: dict[str, Any], locale: str) -> Any:
    address, action = chat.sender(), str(args.get("do") or "status")
    try:
        if action == "status":
            return provider.status(address)
        if action == "start":
            return provider.start(address)
        if action == "poll":
            return provider.poll(address)
        if action == "disconnect":
            return provider.disconnect(address)
        if action == "cancel":
            return provider.cancel(address)
        permitted = {"facebook": {"profile", "posts"}, "tiktok": {"profile", "videos"}}
        if action in permitted[service]:
            return provider.read(address, action)
    except provider.AccountError as exc:
        labels = {"facebook": "Facebook", "tiktok": "TikTok"}
        name = labels[service]
        messages = {
            "not_configured": (f"La conexión oficial de {name} aún no está configurada en el servidor.",
                               f"The official {name} connection is not configured on the server."),
            "not_connected": (f"Todavía no has conectado {name}.", f"You have not connected {name} yet."),
            "network": (f"No pude contactar con {name}. Inténtalo de nuevo.", f"Could not reach {name}. Please retry."),
            "reauthorize": ("La autorización caducó o fue revocada. Conecta tu cuenta de nuevo.",
                            "Authorization expired or was revoked. Connect your account again."),
            "vault": ("No se pudo abrir la sesión cifrada. Comprueba la clave del servidor.",
                      "Could not open the encrypted session. Check the server key."),
            "scope": ("El proveedor no concedió los permisos de lectura esperados, o incluyó otros no solicitados.",
                      "The provider did not grant the expected read permissions, or included unrequested permissions."),
        }
        spanish, english = messages.get(str(exc), (f"{name} no completó la operación. Comprueba la app y vuelve a intentarlo.",
                                                   f"{name} did not complete the operation. Check the app and retry."))
        raise AppError(tr(locale, spanish, english)) from None
    raise AppError(tr(locale, "Operación desconocida.", "Unknown operation."))


@op("facebook_account")
def op_facebook_account(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return _social_account(facebook_account, "facebook", args, locale)


@op("tiktok_account")
def op_tiktok_account(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return _social_account(tiktok_account, "tiktok", args, locale)


@op("code")
def op_code(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Coder: free coding models write, run and fix a small project; the app keeps the files and builds the ZIP."""
    from . import coder

    text = _clean(args.get("text"))[:6000]
    if not text:
        raise AppError(tr(locale, "Describe qué quieres programar.", "Describe what you want to build."))
    files = [item for item in (args.get("files") or [])[:coder.MAX_FILES] if isinstance(item, dict)] if isinstance(args.get("files"), list) else []
    model = _clean(args.get("model"))[:120]
    if model and ai.model_by_key(model) is None:
        raise AppError(tr(locale, "Ese modelo ya no está disponible; elige otro o usa Automático.",
                          "That model is no longer available; pick another or use Automatic."))
    address = chat.sender()
    state.charge_once()  # a project is the unit: its many model calls are not counted one by one
    if state.acting_as():
        if not usage.allowed(address, "code"):
            raise AppError(usage.limit_text("code", locale))
        if usage.reserve_low():
            raise AppError(usage.reserve_text(locale))
    try:
        result = coder.run(text, files, locale, model)
    except coder.CoderError as exc:
        raise AppError(str(exc))
    if result.get("files") and address:
        usage.spend(address, "code")
    result["models"] = models_view()
    return result


MAX_ATTACHMENT = 88_000  # base64 grows it to ~117 KB; one webxdc update carries 128 KB


def _attachment(item: dict[str, Any], text: str, space: str, locale: str) -> tuple[str, list[tuple[str, bytes]]]:
    """Question plus native media parts for one attached file; documents become extracted text."""
    from . import documents

    name = clean_filename(_clean(item.get("name"))[:120] or "file", "file")
    mime = _clean(item.get("mime"))[:80].lower() or "application/octet-stream"
    try:
        data = base64.b64decode(str(item.get("b64") or ""), validate=True)
    except (ValueError, TypeError):
        raise AppError(tr(locale, "No pude leer el adjunto.", "Could not read the attachment."))
    if not data:
        raise AppError(tr(locale, "El adjunto está vacío.", "The attachment is empty."))
    if len(data) > MAX_ATTACHMENT:
        limit = MAX_ATTACHMENT // 1024
        raise AppError(tr(locale, f"El adjunto supera {limit} KB.", f"The attachment exceeds {limit} KB."))
    model = None if space == "auto" else ai.model_by_key(space)
    kinds = ai.accepts(model)
    suffix = Path(name).suffix.lower()
    if mime.startswith("image/"):
        if "image" not in kinds:
            raise AppError(tr(locale, "Este modelo no lee imágenes. Usa Gemini, Ministral o Automático.",
                              "This model cannot read images. Use Gemini, Ministral or Automatic."))
        return text or tr(locale, "Describe esta imagen.", "Describe this image."), [(mime, data)]
    if mime.startswith("audio/"):
        if "audio" not in kinds:
            raise AppError(tr(locale, "Solo los modelos Gemini escuchan audio.", "Only Gemini models can listen to audio."))
        return text or tr(locale, "Transcribe y resume este audio.", "Transcribe and summarize this audio."), [(mime, data)]
    question = text or tr(locale, "Resume este documento.", "Summarize this document.")
    if (mime == "application/pdf" or suffix == ".pdf") and "pdf" in kinds:
        return question, [("application/pdf", data)]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / name
        path.write_bytes(data)
        try:
            document = documents.extract(SimpleNamespace(file=str(path), file_name=name, file_mime=mime))
        except documents.DocumentError as exc:
            raise AppError(documents.error_text(exc, locale).replace("⚠️ ", ""))
    return documents.prompt(question, document), []


def _clock(moment: float) -> str:
    return datetime.fromtimestamp(moment, config.LOCAL_ZONE).strftime("%H:%M") if moment else ""


@op("image")
def op_image(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    prompt = _clean(args.get("prompt"))[:1500]
    if not prompt:
        raise AppError(tr(locale, "Describe la imagen.", "Describe the image."))
    model = _clean(args.get("model"))[:60]
    result = images.generate(prompt, model)
    if result.error == "person_quota":
        raise AppError(usage.limit_text("image", locale))
    if not result.data:
        name = result.source or tr(locale, "El modelo", "The model")
        back = _clock(state.cooldown_until(model)) if model else ""
        reason = {"no_source": tr(locale, "No hay generadores configurados.", "No generators are configured."),
                  "unknown": tr(locale, "Ese modelo ya no está disponible. Elige otro.", "That model is no longer available. Pick another one."),
                  "cooldown": tr(locale, f"{name} está descansando. Elige otro modelo o Automático.", f"{name} is resting. Pick another model or Automatic."),
                  "quota": tr(locale, f"{name}: se agotó la cuota gratis de hoy{f'; vuelve a las {back}' if back else ''}. Elige otro modelo o Automático."
                              if model else "Las cuotas gratis de imagen se agotaron por hoy.",
                              f"{name}: today's free quota is used up{f'; back at {back}' if back else ''}. Pick another model or Automatic."
                              if model else "Free image quotas are used up for today."),
                  "busy": tr(locale, f"{name} está saturado ahora. Prueba en un minuto u otro modelo.", f"{name} is overloaded right now. Try again in a minute or another model."),
                  "auth": tr(locale, f"{name}: el servicio rechazó la clave. Hay que renovarla.", f"{name}: the service rejected the key. It needs to be renewed.")}
        fallback = tr(locale, f"{name} no respondió. Prueba otra vez u otro modelo.", f"{name} did not answer. Try again or another model.") if model else \
            tr(locale, "Los generadores no respondieron.", "The generators did not answer.")
        raise AppError(reason.get(result.error, fallback))
    preview = images.compress(result.data, max_side=1024, quality=80)
    return {"b64": base64.b64encode(preview).decode(), "mime": "image/jpeg", "source": result.source, "model": model, "prompt": prompt,
            "size": len(preview), "models": models_view()}


ENGINES = {
    "web": lambda query, locale: tools.search(query, locale, 20),
    "web+": lambda query, locale: tools.search(query, locale, 20, "advanced"),
    "news": lambda query, locale: tools.news_results(query, locale, 15),
    "videos": lambda query, locale: tools.video_search(query, locale, 15),
}


@op("search")
def op_search(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    query = _clean(args.get("q"))[:300]
    engine = str(args.get("engine") or "web")
    advanced = engine == "web" and args.get("mode") == "advanced"
    if engine == "news" and not query:
        query = tr(locale, "Cuba", "world")
    if not query:
        raise AppError(tr(locale, "¿Qué busco?", "What should I search?"))
    if engine == "wiki":
        found = tools.wiki_summary(query, locale)
        results = [tools.Result(found[0], found[2], found[1], "Wikipedia")] if found else []
    else:
        try:
            results = ENGINES["web+" if advanced else engine if engine in ENGINES else "web"](query, locale)
        except Exception as exc:
            log.info("search failed: %s", exc)
            results = []
    return {"engine": engine, "q": query, "mode": "advanced" if advanced else "standard", "left": tools.you_left() if advanced else None,
            "results": [{"title": item.title, "url": item.url, "snippet": item.snippet, "source": item.source, "date": item.date} for item in results]}


@op("read")
def op_read(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    url = _clean(args.get("url"))
    number = int(args.get("page") or 1)
    try:
        from . import net

        net.validate_public_url(url)
        loaded = (reader.cached(url) if number > 1 else None) or reader.load(url)
    except reader.NotHtml:
        return {"file": True, "url": url, **run_captured(lambda: web.route(chat_id, url, locale), locale)}
    except Exception as exc:
        raise AppError(web.url_error(exc, locale).replace("⚠️ ", ""))
    return reader.app_page(loaded, number) | {"media": bool(media.available() and media.supported(loaded.url))}


ACTIONS: dict[str, Callable[[int, str, str, int], None]] = {
    "pdf": lambda chat_id, url, locale, quality: web.pdf(chat_id, url, locale),
    "capture": lambda chat_id, url, locale, quality: web.capture(chat_id, url, locale),
    "download": lambda chat_id, url, locale, quality: web.enqueue_download(chat_id, url, locale),
    "info": lambda chat_id, url, locale, quality: web.media_card(chat_id, url, locale),
    "video": lambda chat_id, url, locale, quality: web.enqueue_media(chat_id, url, "video", quality or int(state.setting("video_height")), locale),
    "audio": lambda chat_id, url, locale, quality: web.enqueue_media(chat_id, url, "audio", quality or int(state.setting("audio_kbps")), locale),
}


@op("action")
def op_action(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    kind, url = str(args.get("kind") or ""), _clean(args.get("url"))
    quality = int(args.get("quality") or 0)
    if kind == "transcript":
        try:
            title, text = extras.transcript(url, locale)
        except ValueError as exc:
            reason = tr(locale, "Ese video no tiene subtítulos.", "That video has no subtitles.") if str(exc) == "no_subtitles" else media.describe_error(str(exc), locale)
            raise AppError(reason.replace("⚠️ ", ""))
        return {"title": title, "text": text[:200_000]}
    if kind not in ACTIONS or not url:
        raise AppError(tr(locale, "Acción no válida.", "Invalid action."))
    return run_captured(lambda: ACTIONS[kind](chat_id, url, locale, quality), locale) | {"jobs": jobs_view(chat_id, locale)}


@op("command")
def op_command(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Run a tap command from a reply; reader links open inside the app."""
    text = _clean(args.get("text"))[:2000]
    parsed = commands.parse(text)
    if parsed and parsed[0] in {"web", "more"} and parsed[1]:
        url = links.resolve(parsed[1][0])
        if url:
            page = int(parsed[1][1]) if parsed[0] == "more" and len(parsed[1]) > 1 and parsed[1][1].isdigit() else 1
            return {"page": op_read(chat_id, {"url": url, "page": page}, locale)}
    reply = run_captured(lambda: commands.dispatch(chat_id, text, locale), locale)
    return reply | {"jobs": jobs_view(chat_id, locale)}


TOOLS: dict[str, Callable[[str, str], str]] = {
    "weather": tools.weather_text, "time": extras.time_text, "convert": extras.convert_text,
    "calc": tools.calc_text, "rate": tools.rate_text,
}


@op("tool")
def op_tool(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    name = str(args.get("name") or "")
    if name not in TOOLS:
        raise AppError(tr(locale, "Herramienta desconocida.", "Unknown tool."))
    return {"text": TOOLS[name](_clean(args.get("arg"))[:300], locale)}


@op("rates")
def op_rates(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    try:
        rates = tools.current_rates()
    except Exception:
        raise AppError(tr(locale, "No pude consultar elTOQUE ahora.", "elTOQUE is unavailable right now."))
    return {"rates": rates, "at": time.time()}


@op("translate")
def op_translate(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    text = _clean(args.get("text"))[:4500]
    if not text:
        return {"text": "", "detected": ""}
    target = str(args.get("target") or ("en" if locale == "es" else "es"))
    translated, detected = extras.translate(text, target, str(args.get("source") or "auto"))
    if not translated:
        raise AppError(tr(locale, "Los traductores no respondieron. Prueba más tarde.", "The translators did not answer. Try again later."))
    return {"text": translated, "detected": detected}


@op("queue")
def op_queue(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    action, job_id = str(args.get("do") or ""), str(args.get("id") or "")
    message = ""
    if action == "pause":
        message = queue.hold(job_id, True, locale)
    elif action == "resume":
        message = queue.hold(job_id, False, locale)
    elif action == "cancel":
        message = queue.cancel(job_id, locale)
    elif action == "retry":
        message = queue.retry(job_id, locale)
    elif action == "resend":
        message = queue.resend(job_id, int(args.get("part") or 1), locale)
    elif action == "clear":
        message = queue.clear_finished(chat_id, locale)
    return {"jobs": jobs_view(chat_id, locale), "message": message}


@op("settings")
def op_settings(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    changes = args.get("set") if isinstance(args.get("set"), dict) else {}
    if args.get("reset"):
        state.reset_own_settings()
    for key, value in changes.items():
        if key not in commands.SETTINGS:
            continue
        if not state.can_change(key):
            # Shared settings affect everyone, so only administrators change them.
            raise AppError(tr(locale, "Solo el administrador puede cambiar este ajuste.", "Only the administrator can change this setting."))
        parser = commands.SETTINGS[key][2]
        parsed = parser(str(value).lower())
        if parsed is None or not state.set_setting(key, parsed):
            raise AppError(tr(locale, "Valor no válido.", "Invalid value."))
    if "pin" in args:
        pin = str(args.get("pin") or "")
        state.set_setting("ai_pin", pin if pin in ai.provider_keys() else "")
    return settings_view(locale)


@op("health")
def op_health(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return health_view()


@op("notas")
def op_notas(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """B1: list, add or remove the owner’s notes."""
    do = str(args.get("do") or "list")
    if do == "list":
        notes = state.list_notes(chat.sender())
        return {"notes": notes}
    if do == "add":
        text = str(args.get("text") or "").strip()
        if not text:
            raise AppError(tr(locale, "Escribe el texto de la nota.", "Write the note text."))
        note_id = state.add_note(chat.sender(), text)
        return {"ok": True, "id": note_id}
    if do == "delete":
        note_id = int(args.get("id") or 0)
        if not note_id:
            raise AppError(tr(locale, "ID de nota requerido.", "Note ID required."))
        ok = state.delete_note(chat.sender(), note_id)
        return {"ok": ok}
    if do == "clear":
        count = state.clear_notes(chat.sender())
        return {"ok": True, "cleared": count}
    raise AppError(tr(locale, "Operación desconocida.", "Unknown operation."))


@op("enlaces")
def op_enlaces(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """B2: listar, agregar o procesar siguiente enlace pendiente."""
    do = str(args.get("do") or "list")
    if do == "list":
        links_q = state.list_links(chat.sender())
        return {"links": links_q}
    if do == "add":
        url = str(args.get("url") or "").strip()
        title = str(args.get("title") or "").strip()[:100]
        if not url:
            raise AppError(tr(locale, "URL requerida.", "URL required."))
        link_id = state.add_link(chat.sender(), url, title)
        return {"ok": True, "id": link_id}
    if do == "next":
        item = state.next_link(chat.sender())
        if not item:
            return {"ok": False, "empty": True}
        return {"ok": True, "link": item}
    if do == "delete":
        link_id = int(args.get("id") or 0)
        if not link_id:
            raise AppError(tr(locale, "ID de enlace requerido.", "Link ID required."))
        ok = state.remove_link(chat.sender(), link_id)
        return {"ok": ok}
    if do == "clear":
        count = state.clear_links(chat.sender())
        return {"ok": True, "cleared": count}
    raise AppError(tr(locale, "Operación desconocida.", "Unknown operation."))


@op("nauta_strict")
def op_nauta_strict(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """A1: activar/desactivar modo Nauta estricto."""
    value = args.get("value")
    if isinstance(value, bool):
        state.set_nauta_strict(chat.sender(), value)
    elif isinstance(value, str):
        state.set_nauta_strict(chat.sender(), value.lower() in {"1", "true", "yes", "on", "sí", "si"})
    active = state.get_nauta_strict(chat.sender())
    delivery = mail_profiles.effective_settings(chat.sender(), {"part_mb": int(state.setting("part_mb"))})
    part_mb = min(delivery["part_mb"], 10) if active else delivery["part_mb"]
    return {"active": active, "part_mb": part_mb}


@op("kill_switch")
def op_kill_switch(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """F2: activar/desactivar kill switch (pausa cola + IA + descargas)."""
    value = args.get("value")
    if isinstance(value, bool):
        state.set_kill_switch(chat.sender(), value)
    elif isinstance(value, str):
        state.set_kill_switch(chat.sender(), value.lower() in {"1", "true", "yes", "on", "sí", "si"})
    active = state.get_kill_switch(chat.sender())
    queue.wake()
    return {"active": active}


@op("estado")
def op_estado(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """F1: one-line health summary for the app."""
    address = chat.sender()
    active = queue.active_count()
    qbytes = queue.queue_bytes()
    killed = state.get_kill_switch(address)
    nauta = state.get_nauta_strict(address)
    notes = len(state.list_notes(address))
    links_q = len(state.list_links(address))

    parts_es = []
    parts_en = []
    parts_es.append("🟢 Sesión OK" if not killed else "🛑 Kill switch ACTIVO")
    parts_en.append("🟢 Session OK" if not killed else "🛑 Kill switch ON")
    parts_es.append(f"cola {active} activa{(' · ' + human_size(qbytes)) if qbytes else ''}")
    parts_en.append(f"queue {active} active{(' · ' + human_size(qbytes)) if qbytes else ''}")
    parts_es.append(f"notas {notes} · enlaces {links_q}")
    parts_en.append(f"notes {notes} · links {links_q}")
    parts_es.append(f"Nauta {'estricto' if nauta else 'normal'}")
    parts_en.append(f"Nauta {'strict' if nauta else 'normal'}")

    return {
        "es": " · ".join(parts_es),
        "en": " · ".join(parts_en),
        "killed": killed,
        "nauta_strict": nauta,
        "active_jobs": active,
        "notes": notes,
        "links": links_q,
    }


@op("owner_only")
def op_owner_only(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    if not admin.is_admin(chat.sender()) or chat.sender().strip().lower() != config.OWNER_ADDRESS:
        raise AppError(tr(locale, "Solo el dueño puede cambiar el acceso.", "Only the owner can change access."))
    if "enabled" in args:
        enabled, minutes = args.get("enabled"), args.get("minutes", 60)
        if type(enabled) is not bool or type(minutes) is not int or minutes not in access.DURATIONS:
            raise AppError(tr(locale, "Modo o duración inválidos.", "Invalid mode or duration."))
        if not access.set_owner_only(enabled, minutes, str(args.get("nonce") or "")):
            raise AppError(tr(locale, "No se puede compartir durante mantenimiento o la sesión cambió. Actualiza Administración.",
                              "Sharing is unavailable during maintenance or the session changed. Refresh Administration."))
        queue.wake()
    return admin.snapshot() | {"usage": usage.report(), "perf": perf.snapshot(), "message": ""}


@op("admin")
def op_admin(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    if not admin.is_admin(chat.sender()):
        raise AppError(tr(locale, "Solo para administradores.", "Administrators only."))
    action, address = str(args.get("do") or ""), _clean(args.get("address")).lower()
    message = ""
    if action in {"allow", "remove", "block"}:
        done = admin.grant(address) if action == "allow" else admin.revoke(address, block=action == "block")
        queue.wake()
        message = tr(locale, "Hecho.", "Done.") if done else tr(locale,
            "No se pudo cambiar: revisa la dirección, el modo privado y la conexión de Delta Chat.",
            "Could not change access: check the address, private mode and Delta Chat connection.")
    elif action == "alerts":
        admin.set_alerts(bool(args.get("value")))
    elif action == "clear_logs":
        admin.clear_logs()
        message = tr(locale, "Registro de esta sesión vaciado.", "Session log cleared.")
    elif action == "restart":
        message = admin.restart(locale)
    elif action == "purge":
        preserve = access.preserve_history()
        removed = chat.purge_outbox(chat_id=chat_id if preserve else None)
        message = tr(locale, f"{removed} envíos pendientes eliminados" + (" de este chat." if preserve else "."),
                     f"{removed} pending messages removed" + (" from this chat." if preserve else "."))
    elif action == "pause_queue":
        state.set_flag("queue_paused", bool(args.get("value")))
        queue.wake()
    elif action in {"limit", "unlimit"} and "@" in address:
        limited = set(state.flag("limited") or [])
        limited.add(address) if action == "limit" else limited.discard(address)
        state.set_flag("limited", sorted(limited))
        queue.wake()
        message = tr(locale, "Hecho.", "Done.")
    elif action == "reset_usage":
        usage.clear()
    elif action == "bench":
        result = perf.bench()
        message = tr(locale, "Ya hay una prueba en marcha.", "A test is already running.") if result.get("busy") else tr(locale, "Prueba terminada.", "Test finished.")
    return admin.snapshot() | {"usage": usage.report(), "perf": perf.snapshot(), "message": message.replace("🔄 ", "").replace("⚠️ ", "")}

