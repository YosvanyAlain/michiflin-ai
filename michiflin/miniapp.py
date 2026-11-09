"""Mini app bridge: the bot ships the webxdc app and answers its requests through status updates.

App -> bot: {"t": "req", "id": str, "op": str, "args": {...}, "lang": "es"|"en"}
            {"t": "req_chunk", "id": str, "i": n, "n": total, "s": base64} (large coding projects)
Bot -> app: {"t": "res", "re": id, "ok": bool, "data": ..., "error": str}
            {"t": "chunk", "re": id, "i": n, "n": total, "s": str}  (oversized responses, reassembled by the app)
            {"t": "push", "kind": str, "data": ...}                 (queue changes, alerts)
            {"t": "ack", "re": id}                                    (slow request still running)
Requests carry "retry": n and the current process session. Cached answers can be resent; after a restart or
older-backup restore, stale work and mutations are refused rather than repeated automatically.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
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

from . import __version__, access, admin, agent, ai, answer_inline, app_compat, browser, chat, commands, config, extras, facebook_account, filekit, filekit_api, handover, identity_guard, images, links, maintenance, mail_profiles, media, music, perf, queue, rate_store, reader, smart_agent, state, tiktok_account, tools, usage, weather_home, web, web_vault, youtube_account
from .util import clean_filename, compact, human_size, tr

log = logging.getLogger("michiflin")
APP_DIR = Path(__file__).resolve().parent.parent / "webxdc"
APP_FILES = (
    "index.html", "manifest.toml", "icon.png", "app.css", "i18n.js", "core.js", "mp4.js", "brands.js", "app.js", "live.js", "coder.js",
    "filekit-store.js", "filekit-worker.js", "filekit-join.js", "filekit.js",
    "vendor/7zip/sevenzip-wasm.js", "vendor/7zip/sevenzip-wasm.wasm", "vendor/7zip/LICENSE", "vendor/7zip/NOTICE.txt",
    "vendor/7zip/source-7zip-26.05.tar.gz", "vendor/7zip/source-wrapper-26.5.0.tar.gz",
    "vendor/noble/filekit-crypto.js", "vendor/noble/LICENSE-ciphers.txt", "vendor/noble/LICENSE-curves.txt",
    "vendor/noble/LICENSE-hashes.txt", "vendor/noble/NOTICE.txt",
)
CHUNK = 90_000
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="app")
# Administrative controls cannot be starved by ordinary owner or guest work.
_control_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-admin")
_owner_work_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-owner")
_guest_control_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-pause")
_dispatch_lock = threading.Lock()
_dispatch_pending: dict[str, tuple[str, str]] = {}  # ticket -> (lane, person)
_LANE_LIMITS = {"normal": 64, "owner": 16, "owner_work": 32, "pause": 32}
_PERSON_LIMITS = {"normal": 8, "owner": 16, "owner_work": 32, "pause": 2}
_instance_locks: dict[int, threading.Lock] = {}
# Welcome and explicitly requested recovery must decide and hand off together.
_upgrade_lock = threading.Lock()
_job_marks: dict[str, tuple[str, int]] = {}
# Exact pending pages stay only in process memory, isolated by app, grant and recipient.
_inline_pages = answer_inline.InlinePages()
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


def compatible_build(entry: dict[str, Any]) -> bool:
    """Admit only this package and the exact previously tested builds."""
    value = entry.get("build")
    if not isinstance(value, str) or not value:
        return False
    if value == app_build():
        return True
    latest = app_compat.latest_build(config.BOT_EPOCH, str(APP_DIR), APP_FILES)
    if latest and value == latest:
        return True
    recent = app_compat.recent_build(config.BOT_EPOCH, str(APP_DIR), APP_FILES)
    if recent and value == recent:
        return True
    previous = app_compat.previous_build(config.BOT_EPOCH, str(APP_DIR), APP_FILES)
    return bool(previous and value == previous)


def discloses_infobars(entry: dict[str, Any]) -> bool:
    """Only packages with the specific graphical-browser warning can hide it."""
    value = entry.get("build")
    latest = app_compat.latest_build(config.BOT_EPOCH, str(APP_DIR), APP_FILES)
    recent = app_compat.recent_build(config.BOT_EPOCH, str(APP_DIR), APP_FILES)
    return bool(isinstance(value, str) and value and (value == app_build() or (latest and value == latest) or (recent and value == recent)))


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


def _verified_binding(entry: dict[str, Any]) -> bool:
    if not (config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS):
        return True
    try:
        return identity_guard.authorized_entry(chat.rpc(), chat.account(), entry)
    except Exception:
        return False


def send_app(chat_id: int, locale: str, updated: bool = False, address: str = "", interrupted: Any = None,
             bilingual_welcome: bool = False) -> int:
    recipient = address or chat.sender()
    if not _verified_binding({"chat": chat_id, "addr": recipient}):
        return 0
    if updated:
        text = tr(locale, "✨ Michiflin AI se actualizó. La app anterior se conserva con sus proyectos e historial; "
                          "esta nueva instancia tiene almacenamiento independiente.",
                  "✨ Michiflin AI was updated. The previous app and its projects and history are kept; "
                  "this new instance has separate storage.")
    else:
        spanish = ("👋 ¡Hola! Soy Michiflin AI. Descarga esta miniapp y ábrela con «Iniciar» o su icono. "
                   "Dentro encontrarás IA, web, descargas y ajustes; las respuestas llegan por correo.")
        english = ("👋 Hi! I'm Michiflin AI. Download this mini app, then open it with “Start” or its icon. "
                   "Inside you'll find AI, web, downloads and settings; replies arrive by email.")
        text = spanish + "\n\n" + english if bilingual_welcome else tr(locale, spanish, english)
    # An unknown-language first welcome still needs to carry the maintenance
    # notice for both audiences, without creating another chat message.
    languages = ("es", "en") if bilingual_welcome and not updated else (locale,)
    for language in languages:
        advisory = maintenance.text(language)
        if advisory:
            text += "\n\n🛠️ " + advisory
    send_args: dict[str, Any] = {"file": build(), "filename": "Michiflin AI.xdc", "viewtype": "Webxdc"}
    if config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS:
        def interrupted_or_changed() -> bool:
            return bool((interrupted is not None and interrupted()) or not admin.is_allowed(recipient)
                        or not _verified_binding({"chat": chat_id, "addr": recipient}))
        send_args["interrupted"] = interrupted_or_changed
    elif interrupted is not None:
        send_args["interrupted"] = interrupted
    msg_id = chat.send(chat_id, text, **send_args)
    if msg_id:
        with state.lock:
            recipient = address or chat.sender()
            _apps()[str(msg_id)] = {"chat": chat_id, "serial": 0, "addr": recipient, "sent": time.time(), "locale": locale,
                                    "build": app_build(), "epoch": config.BOT_EPOCH,
                                    "access_session": access.session_stamp(recipient)}
            # Every WebXDC instance owns its local storage. Dropping an old binding can
            # lead the history pruner to delete its bot-side message and strand projects.
            # Keep even older instances until the user explicitly deletes them.
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
    """Count work still running, including workers whose reply already timed out."""
    with _answers_lock:
        answers = sum(1 for item in _answers.values() if item.get("state") == "running")
    with _dispatch_lock:
        pending = len(_dispatch_pending)
    return max(answers, pending)


_pointed: dict[int, float] = {}


def latest_instance(chat_id: int) -> int:
    """Newest app message of this chat that still exists, or 0."""
    with state.lock:
        keys = sorted((int(key) for key, entry in _apps().items() if int(entry["chat"]) == chat_id
                       and _instance(int(key)) is not None and access.bound_allowed(entry)), reverse=True)
    for msg_id in keys:
        try:
            message = chat.rpc().get_message(chat.account(), msg_id)
        except Exception:
            message = None
        if message is not None and int(message.chat_id) == chat_id:
            return msg_id
        # An RPC failure is not evidence that an app or its local data disappeared.
        # Preserve the binding so a transient error cannot make it eligible for pruning.
    return 0


def point_to_app(chat_id: int, locale: str) -> None:
    """Point at an app, or deliver one only when no current binding is known."""
    with _upgrade_lock:
        msg_id = latest_instance(chat_id)
        bound = _bound_apps(chat_id)
        known = _registered_app_for_sender(chat_id)
        if not msg_id and not bound and not known:
            # First delivery only. An earlier registration, even one whose
            # access was revoked, cannot justify an automatic replacement.
            send_app(chat_id, locale)
            return
    if time.time() - _pointed.get(chat_id, 0) < 180:
        return
    _pointed[chat_id] = time.time()
    if msg_id:
        text = tr(locale, "Michiflin funciona dentro de la mini app de este chat: ábrela con «Iniciar» y pide ahí lo que necesites.",
                  "Michiflin works inside the mini app in this chat: open it with “Start” and ask for what you need there.")
    elif known and not bound:
        # A new grant must not revive an old app or replay its requests.
        text = tr(locale, "Hay una miniapp anterior registrada en este chat, pero su acceso previo no se ha reactivado. No envié otra automáticamente; si necesitas una nueva, consulta al propietario.",
                  "An earlier mini app is registered in this chat, but its previous access was not reactivated. I did not send another automatically; if you need a new one, ask the owner.")
    else:
        # An RPC failure is not proof the previous message disappeared.
        # Keep the existing instance instead of automatically creating another.
        text = tr(locale, "No pude localizar la mini app ahora. La que ya tienes no se ha eliminado; vuelve a abrirla.",
                  "I could not locate the mini app now. Your existing instance was not deleted; try opening it again.")
    chat.send(chat_id, text, quote=msg_id or None)


def welcome(chat_id: int, locale: str) -> None:
    """First contact: one app message; use both languages only if unknown."""
    if maintenance.silent_active():
        return
    with _upgrade_lock:
        # A new build, a repeated secure-join, or a renewed grant is not
        # permission to create another app. A historical registration blocks
        # delivery but does not itself grant the old instance any access.
        if _bound_apps(chat_id) or _registered_app_for_sender(chat_id):
            return
        if locale in {"es", "en"}:
            send_app(chat_id, locale)
        else:
            send_app(chat_id, "es", bilingual_welcome=True)


def _instance(msg_id: int) -> dict[str, Any] | None:
    """Known instance of this deployment; old builds can sync and request an update explicitly."""
    entry = _apps().get(str(msg_id))
    if entry is None:
        return None
    # Another deployment's packages must not drive this bot at all.
    epoch = entry.get("epoch")
    if (config.BOT_EPOCH_STRICT and epoch != config.BOT_EPOCH) or (epoch and epoch != config.BOT_EPOCH):
        return None
    return entry


def _bound_apps(chat_id: int) -> list[dict[str, Any]]:
    """Registered, currently authorized instances in this identity and chat.

    A missing or unreadable core message is not evidence that the owner's
    instance-local data is gone. Callers still allow explicit /app recovery.
    """
    entries: list[dict[str, Any]] = []
    with state.lock:
        for key, entry in _apps().items():
            if not str(key).isdigit() or not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("chat") or 0) != chat_id or _instance(int(key)) is None or not access.bound_allowed(entry):
                    continue
            except (TypeError, ValueError):
                continue
            entries.append(dict(entry))
    return entries


def _registered_app_for_sender(chat_id: int) -> bool:
    """Detect an earlier app without authorizing it or reading its messages.

    The chat, deployment epoch and recipient must match. An unknown recipient
    is treated conservatively: it blocks automatic delivery, never grants access.
    Different recipients or deployments do not suppress a legitimate first app.
    """
    recipient = str(chat.sender() or "").strip().casefold()
    with state.lock:
        for key, entry in _apps().items():
            if not str(key).isdigit() or not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("chat") or 0) != chat_id or _instance(int(key)) is None:
                    continue
            except (TypeError, ValueError):
                continue
            address = str(entry.get("addr") or "").strip().casefold()
            if not recipient or not address or address == recipient:
                return True
    return False


def observed_locale(chat_id: int) -> str:
    """Return a language actually sent by an authorized app, not a guessed default."""
    observed: list[tuple[float, str]] = []
    for entry in _bound_apps(chat_id):
        language = entry.get("locale")
        try:
            active = float(entry.get("active") or 0)
        except (TypeError, ValueError):
            continue
        if entry.get("language_observed") is True and active > 0 and language in {"es", "en"}:
            observed.append((active, str(language)))
    return max(observed, default=(0.0, ""))[1]


SAFE_REPLAY = {"hello", "models", "health", "repair"}  # no provider calls or billable side effects
BOOT_SESSION = secrets.token_urlsafe(24)  # deliberately never stored in a backup
ACK_AFTER = 12.0        # seconds before a still-running request is acknowledged (one small update)
SWEEP_EVERY = 30.0      # catch updates whose event was missed (restarts, reconnects)
ANSWER_CACHE = 2400.0   # seconds an answer stays ready for a resend
ANSWER_ITEM_BYTES = 3_000_000
ANSWER_TOTAL_BYTES = 24_000_000  # bounded RAM; never write responses or keys to the state backup
SEEN_AGE = 172800
SEEN_LIMIT = 100_000
SEEN_PER_PERSON = 10_000  # over 48 hours; never evict a live replay-protection entry
SEEN_ADMIN_RESERVE = 10_000
_seen_counts: dict[str, int] = {}
_seen_ref: Any = None
_seen_size = -1
UPGRADE_GAP = 1800.0    # suppress duplicate sends of the same build, not later builds
_answers: dict[str, dict[str, Any]] = {}
_answers_lock = threading.Lock()
_upgraded: dict[tuple[int, str], float] = {}


def _seen() -> dict[str, list[float]]:
    """Request ids already admitted: id -> [time, retry]. Persisted, so replays after a restart are recognized."""
    return state.data.setdefault("app_seen", {})


def _seen_owner(instance_id: int) -> str:
    entry = _apps().get(str(instance_id))
    return str(entry.get("addr") or "").strip().lower() if isinstance(entry, dict) else ""


def _count_seen(seen: dict[str, list[float]]) -> dict[str, int]:
    global _seen_ref, _seen_size
    if _seen_ref is not seen or _seen_size != len(seen):
        _seen_counts.clear()
        for item in seen.values():
            try:
                owner = _seen_owner(int(item[2])) if len(item) > 2 else ""
            except (TypeError, ValueError):
                owner = ""
            if owner:
                _seen_counts[owner] = _seen_counts.get(owner, 0) + 1
        _seen_ref, _seen_size = seen, len(seen)
    return _seen_counts


def _prune_seen() -> None:
    global _seen_ref
    now = time.time()
    with state.lock:
        seen = _seen()
        for key, item in list(seen.items()):
            if now - float(item[0]) > SEEN_AGE:
                seen.pop(key, None)
        _seen_ref = None
    with _answers_lock:
        for key in [key for key, item in _answers.items() if now - item["ts"] > ANSWER_CACHE]:
            _answers.pop(key, None)
    _inline_pages.prune()
    from . import local_ai
    local_ai.prune_memory()
    # Partial coding requests contain user files. Remove expired fragments even
    # when no new request ever arrives, rather than keeping them in backups.
    changed = False
    with state.lock:
        for entry in _apps().values():
            parts = entry.get("request_parts") if isinstance(entry, dict) else None
            if not isinstance(parts, dict):
                continue
            for key, item in list(parts.items()):
                try:
                    expired = not isinstance(item, dict) or now - float(item.get("at", 0)) > REQUEST_AGE
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    parts.pop(key, None)
                    changed = True
    if changed:
        state.save()


def _admit(rid: str, retry: int, instance_id: int = 0) -> str:
    """Do not return cached results to a different WebXDC instance, even on ID collision."""
    global _seen_ref, _seen_size
    with state.lock:
        seen = _seen()
        known = seen.get(rid)
        if known is None:
            owner = _seen_owner(instance_id)
            reserve = SEEN_ADMIN_RESERVE if config.INVITE_AUTO_ACCESS and admin.is_admin(owner) else 0
            counts = _count_seen(seen)
            person_full = bool(config.INVITE_AUTO_ACCESS and owner and not admin.is_admin(owner)
                               and counts.get(owner, 0) >= SEEN_PER_PERSON)
            if len(seen) >= SEEN_LIMIT + reserve or person_full:
                now = time.time()
                for key, item in list(seen.items()):
                    if now - float(item[0]) > SEEN_AGE:
                        seen.pop(key, None)
                _seen_ref = None
                counts = _count_seen(seen)
                person_full = bool(config.INVITE_AUTO_ACCESS and owner and not admin.is_admin(owner)
                                   and counts.get(owner, 0) >= SEEN_PER_PERSON)
                if len(seen) >= SEEN_LIMIT + reserve or person_full:
                    return "full"  # never forget a recent potentially paid request
            seen[rid] = [time.time(), retry, instance_id]
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
            _seen_size = len(seen)
            fresh = True
        else:
            fresh = False
            if len(known) > 2 and int(known[2]) != instance_id:
                return "replay"
            if len(known) < 3:
                known.append(instance_id)  # upgrade a legacy seen entry on its first retry
                _seen_ref = None
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


# Bound and persist partial request chunks together with the app serial. If the
# bot restarts between updates, the remaining pieces can still complete the
# same request without executing it twice. Chunks are accepted only from an
# already-bound instance; normal authorization is rechecked in handle().
REQUEST_MAX = 600_000
REQUEST_PART = 74_000
REQUEST_PARTS = 12
REQUEST_AGE = 3600


def _request_piece(entry: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    rid, part = payload.get("id"), payload.get("s")
    index, total = payload.get("i"), payload.get("n")
    if (not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9]{5,40}", rid) or
            not isinstance(part, str) or len(part) > REQUEST_PART or not re.fullmatch(r"[A-Za-z0-9+/=]+", part) or
            type(index) is not int or type(total) is not int or not 1 <= total <= REQUEST_PARTS or not 0 <= index < total):
        return None
    with state.lock:
        parts = entry.setdefault("request_parts", {})
        if not isinstance(parts, dict):
            entry["request_parts"] = parts = {}
        now = time.time()
        for key, item in list(parts.items()):
            if not isinstance(item, dict) or now - float(item.get("at", 0)) > REQUEST_AGE:
                parts.pop(key, None)
        saved = parts.get(rid)
        if saved is None:
            if len(parts) >= 2:  # at most two incomplete projects per instance
                return None
            saved = parts[rid] = {"at": now, "n": total, "pieces": {}}
        if saved.get("n") != total or not isinstance(saved.get("pieces"), dict):
            parts.pop(rid, None)
            return None
        pieces = saved["pieces"]
        key = str(index)
        if key in pieces and pieces[key] != part:
            parts.pop(rid, None)
            return None
        pieces[key] = part
        if len(pieces) != total:
            return None
        parts.pop(rid, None)
        try:
            encoded = "".join(pieces[str(i)] for i in range(total))
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > REQUEST_MAX:
                return None
            request = json.loads(raw.decode("utf-8"))
            if (not isinstance(request, dict) or request.get("t") != "req" or request.get("id") != rid
                    or request.get("op") != "code"):
                return None
            return request
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None


def on_status_update(msg_id: int) -> None:
    """Read new updates of an instance and dispatch the app's requests."""
    if _paused.is_set():
        return
    lock = _instance_locks.setdefault(msg_id, threading.Lock())
    with lock:
        entry = _instance(msg_id)
        if entry is None or not access.bound_allowed(entry) or not _verified_binding(entry):
            return  # old, revoked or unverified instances cannot replay pending requests
        try:
            raw = chat.rpc().get_webxdc_status_updates(chat.account(), msg_id, int(entry.get("serial", 0)))
            updates = json.loads(raw or "[]")
        except Exception as exc:
            log.warning("webxdc updates unreadable: %s", exc)
            return
        if not updates:
            return
        for update in updates:
            serial = int(update.get("serial", 0))
            payload = update.get("payload")
            try:
                if handover.hold_update(payload, BOOT_SESSION):
                    handover.remember("updates", f"{msg_id}:{serial}",
                                      {"instance_id": msg_id, "serial": serial,
                                       "request_id": str(payload.get("id") or "")[:40]})
                    entry["serial"] = max(int(entry.get("serial", 0)), serial)
                    continue
            except (handover.FenceNotReady, OSError, TypeError, ValueError) as exc:
                log.warning("handover update deferred: %s", exc.__class__.__name__)
                return  # unread update can be retried after the marker is ready
            entry["serial"] = max(int(entry.get("serial", 0)), serial)
            if isinstance(payload, dict):
                if payload.get("t") == "req_chunk":
                    address = str(entry.get("addr", "")).strip().lower()
                    if (not address or not admin.is_allowed(address) or not config.service_access_allowed(address)
                            or not compatible_build(entry)):
                        continue
                    payload = _request_piece(entry, payload)
                if isinstance(payload, dict) and payload.get("t") == "req":
                    entry["active"] = time.time()
                    # The UI chooses its own language per instance. Remember the
                    # most recently observed language for future chat notices;
                    # do not change the device or any other instance's settings.
                    address = str(entry.get("addr", "")).strip().lower()
                    language = payload.get("lang")
                    if (isinstance(language, str) and language in {"es", "en"} and address and access.bound_allowed(entry)
                            and admin.is_allowed(address) and config.service_access_allowed(address)):
                        with state.lock:
                            entry["locale"] = language
                            entry["language_observed"] = True
                    _route(msg_id, dict(entry), payload)
        state.save()


def _needs_session(op: str, args: dict[str, Any]) -> bool:
    """Only unambiguously read-only requests can use a stale boot session."""
    if op in SAFE_REPLAY or op == "estado":
        return False
    if op in {"queue", "admin"}:
        return bool(args.get("do"))
    if op == "settings":
        return bool(args.get("set")) or bool(args.get("reset")) or "pin" in args
    if op in {"notas", "enlaces"}:
        return str(args.get("do") or "list") != "list"
    if op in {"kill_switch", "nauta_strict"}:
        return "value" in args
    if op == "owner_only":
        return "enabled" in args
    if op == "maintenance_notice":
        return "active" in args
    if op in {"personal_ai", "browser_session"}:
        return str(args.get("do") or "status") != "status"
    return True


def _dispatch_acquire(address: str, operation: str) -> tuple[str, str]:
    is_admin = admin.is_admin(address)
    if is_admin and operation in {"kill_switch", "owner_only", "maintenance_notice", "admin"}:
        lane = "owner"
    elif is_admin and config.INVITE_AUTO_ACCESS:
        lane = "owner_work"
    else:
        lane = "pause" if operation == "kill_switch" else "normal"
    with _dispatch_lock:
        total = sum(1 for kind, _person in _dispatch_pending.values() if kind == lane)
        personal = sum(1 for kind, person in _dispatch_pending.values() if kind == lane and person == address)
        if total >= _LANE_LIMITS[lane] or personal >= _PERSON_LIMITS[lane]:
            return "", lane
        ticket = secrets.token_hex(16)
        _dispatch_pending[ticket] = (lane, address)
        return ticket, lane


def _dispatch_release(ticket: str) -> None:
    if ticket:
        with _dispatch_lock:
            _dispatch_pending.pop(ticket, None)


def _dispatch_handle(instance_id: int, entry: dict[str, Any], request: dict[str, Any]) -> None:
    try:
        handle(instance_id, entry, request)
    finally:
        # If the operation timed out but its underlying worker continues, that
        # worker owns this ticket until it really finishes.
        if not request.get("_dispatch_worker_started"):
            _dispatch_release(str(request.get("_dispatch_ticket") or ""))


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
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    locale = request.get("lang") if request.get("lang") in {"es", "en"} else "es"
    address = str(entry.get("addr", "")).strip().lower()
    authorized = bool(address and access.bound_allowed(entry) and admin.is_allowed(address))
    # The state backup can be older than an already charged request. A new,
    # process-only session token makes even an *unseen* stale request fail
    # closed after restart/restore instead of assuming the ledger is complete.
    if verdict not in {"replay", "full"} and _needs_session(op, args) and request.get("session") != BOOT_SESSION:
        if authorized:
            warning = tr(locale,
                "El bot se reinició o debe sincronizarse. Una solicitud anterior pudo terminar o generar cargos; revisa el proveedor y Descargas antes de repetir.",
                "The bot restarted or needs to sync. A previous request may have completed or incurred charges; check the provider and Downloads before retrying.")
            if state.maintenance_active():
                warning = maintenance_message(locale) + "\n" + warning
            respond(instance_id, rid, None, warning, code="session_changed")
        return
    if verdict == "lost" and op in SAFE_REPLAY:
        with _answers_lock:
            _answers[rid] = {"state": "running", "ts": time.time(), "body": None, "acked": 0.0}
        verdict = "new"
    if verdict == "new":
        ticket, lane = _dispatch_acquire(address, op)
        if not ticket:
            if authorized:
                respond(instance_id, rid, None, tr(locale,
                    "El servidor está atendiendo otras solicitudes. Espera un momento y vuelve a intentarlo.",
                    "The server is handling other requests. Please wait a moment and try again."))
            return
        try:
            with state.lock:
                admitted = dict(request, _kill_generation=state.kill_generation(address),
                                _kill_at_admission=state.get_kill_switch(address),
                                _dispatch_ticket=ticket, _dispatch_lane=lane, _dispatch_worker_started=False)
            pool = {"owner": _control_pool, "owner_work": _owner_work_pool,
                    "pause": _guest_control_pool, "normal": _pool}[lane]
            pool.submit(_dispatch_handle, instance_id, entry, admitted)
        except Exception:
            _dispatch_release(ticket)
            raise
    elif verdict == "running":
        _ack(instance_id, rid, force=True, entry=entry)
    elif verdict == "cached":
        with _answers_lock:
            body = (_answers.get(rid) or {}).get("body")
        if body and authorized:
            log.info("app resend %s answered from cache", op)
            _deliver(instance_id, rid, body)
        elif authorized:
            respond(instance_id, rid, None, tr(locale,
                "La respuesta ya no está disponible para reenviarla. El proveedor pudo cobrar; revisa su historial antes de repetir.",
                "The answer is no longer available for resending. The provider may have charged; check its history before retrying."))
    elif verdict == "lost" and authorized:
        respond(instance_id, rid, None, tr(locale,
            "Se perdió la respuesta. El proveedor pudo cobrar o la operación pudo completarse; revisa su historial y Descargas antes de repetir.",
            "The answer was lost. The provider may have charged or the operation may have completed; check its history and Downloads before retrying."))
    elif verdict == "full" and authorized:
        respond(instance_id, rid, None, tr(locale,
            "Hay demasiadas solicitudes recientes para garantizar reintentos seguros. Espera antes de iniciar otra.",
            "There are too many recent requests to guarantee safe retries. Wait before starting another."))


def _status_allowed(instance_id: int) -> bool:
    """Recheck epoch, access grant and verified contact at the actual handoff."""
    if not (config.BOT_EPOCH_STRICT or config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS):
        return True  # preserve existing deployments until a deliberate cutover
    try:
        entry = _instance(instance_id)
        if entry is None:
            return False
        address = str(entry.get("addr") or "").strip().lower()
        return bool(address and access.bound_allowed(entry) and admin.is_allowed(address) and _verified_binding(entry))
    except Exception:
        return False


def _send_status(instance_id: int, payload: Any, notify: dict[str, str] | None = None) -> bool:
    if config.BOT_EPOCH_STRICT or config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS:
        return chat.send_update(instance_id, payload, notify, interrupted=lambda: not _status_allowed(instance_id))
    return chat.send_update(instance_id, payload, notify) if notify else chat.send_update(instance_id, payload)


def _ack(instance_id: int, rid: str, force: bool = False, entry: dict[str, Any] | None = None) -> None:
    if entry is not None and (not access.bound_allowed(entry) or not _verified_binding(entry)):
        return
    if (config.BOT_VERIFIED_CONTACTS_ONLY or config.EMAIL_KEY_RECOVERY or config.INVITE_AUTO_ACCESS) and entry is None:
        current = _instance(instance_id)
        if current is None or not _verified_binding(current):
            return
    with _answers_lock:
        item = _answers.get(rid)
        if not item or item["state"] != "running" or (not force and item["acked"]) or time.time() - item["acked"] < 20:
            return
        item["acked"] = time.time()
    _send_status(instance_id, {"t": "ack", "re": rid})


WORK_OPS = {"ask", "image", "code", "read", "search", "youtube_search", "music_search", "music_item", "action", "command", "tool", "translate", "rates", "weather",
            "youtube_account", "facebook_account", "tiktok_account", "filekit"}
AI_OPS = {"ask", "image", "code"}
KILL_SAFE_OPS = {"hello", "health", "kill_switch", "owner_only", "maintenance_notice", "nauta_strict", "settings", "models", "estado"}
OP_LIMITS = {"ask": 240, "image": 480, "read": 150, "action": 150, "search": 100, "music_search": 60, "music_item": 60, "translate": 90, "code": 900, "personal_ai": 150, "filekit": 2100}
_workers = ThreadPoolExecutor(max_workers=16, thread_name_prefix="app-op")
_control_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-admin-op")
_owner_work_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="app-owner-op")
_guest_control_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="app-pause-op")
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


def maintenance_message(locale: str) -> str:
    custom = maintenance.text(locale) or tr(locale, "El bot estuvo en mantenimiento.", "The bot was under maintenance.")
    return tr(locale, "🛠️ Mantenimiento: ", "🛠️ Maintenance: ") + custom + tr(locale,
        "\nLas colas autorizadas continuarán al terminar. Una operación ya enviada a un proveedor puede haber terminado o generado cargos; compruébalo antes de repetir.",
        "\nAuthorized queues will continue afterwards. A request already sent to a provider may have completed or incurred charges; check before retrying.")


def _inline_scope(entry: dict[str, Any], args: dict[str, Any]) -> str:
    """Separate cached pages by chat, access grant, application build and mode."""
    parts = [str(entry.get(name) or "")[:200] for name in ("chat", "epoch", "access_session", "build")]
    parts += [str(args.get("space") or "agent")[:180], str(args.get("agent_model") or "")[:180]]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode("ascii")).hexdigest()


def _same_inline_identity(instance_id: int, admitted: dict[str, Any]) -> bool:
    """Recheck the encrypted recipient and exact grant immediately before use."""
    try:
        from . import live_web
        current = live_web._authorized(instance_id)
        if not current:
            return False
        entry, address = current
        return (str(address).strip().casefold() == str(admitted.get("addr") or "").strip().casefold()
                and all(str(entry.get(field) or "") == str(admitted.get(field) or "")
                        for field in ("chat", "epoch", "access_session", "build")))
    except Exception:
        return False


class _InlineContinuation:
    def __init__(self, key: tuple[int, str, str], version: object) -> None:
        self.key, self.version = key, version


def handle(instance_id: int, entry: dict[str, Any], request: dict[str, Any]) -> None:
    admitted_entry = dict(entry)  # retain the original grant and epoch across worker delays
    rid = str(request.get("id", ""))[:40]
    locale = request.get("lang") if request.get("lang") in {"es", "en"} else "es"
    op = str(request.get("op", ""))
    function = OPS.get(op)
    chat_id = int(entry["chat"])
    address = str(entry.get("addr", "")).strip().lower()

    def reply(data: Any, error: str = "") -> None:
        if access.bound_allowed(entry) and admin.is_allowed(address) and _verified_binding(entry):
            respond(instance_id, rid, data, error)

    if not access.bound_allowed(entry) or not _verified_binding(entry):
        return  # never replay or respond to a stale or unverified instance
    if not address or not admin.is_allowed(address):
        reply(None, tr(locale, "Acceso revocado. Solicita una nueva autorización.",
                                           "Access revoked. Please request authorization again."))
        return
    if not config.service_access_allowed(address):
        reply(None, tr(locale, "Michiflin está en mantenimiento y solo lo puede usar su dueño.",
                                           "Michiflin is under maintenance and available only to its owner."))
        return
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    # Maintenance does not rewrite manual pauses or grants. Status and the
    # owner's access/maintenance controls remain usable, but work is refused.
    owner_control = (config.is_owner_admin(address) and op == "admin"
                     and args.get("do", "") in {"", "allow", "remove", "block", "alerts", "pause_queue", "limit", "unlimit"})
    maintenance_safe = (op in {"hello", "health", "maintenance_notice", "owner_only", "kill_switch"}
                        or (op == "queue" and not args.get("do")) or owner_control)
    if state.maintenance_active() and not maintenance_safe:
        reply(None, maintenance_message(locale))
        return
    social_ops = {"youtube_account", "facebook_account", "tiktok_account", "browser_session"}
    kill_safe = (op in KILL_SAFE_OPS or owner_control or (op == "queue" and not args.get("do") and state.maintenance_active())
                 or (op in social_ops and args.get("do", "status") in {"status", "disconnect", "cancel"})
                 or (op == "personal_ai" and args.get("do", "status") in {"status", "remove"}))
    generation = int(request.get("_kill_generation", state.kill_generation(address)))
    was_killed = bool(request.get("_kill_at_admission", state.get_kill_switch(address)))
    if not kill_safe and (was_killed or state.work_interrupted(address, generation)):
        reply(None, maintenance_message(locale) if state.maintenance_interrupted(generation) else kill_message(locale))
        return
    # The exact preceding protocol-compatible build can keep working without
    # creating a new app. Unknown older packages retain only explicit recovery
    # and essential owner controls; all identity and grant checks still apply.
    recovery_control = (config.is_owner_admin(address) and (
        op in {"owner_only", "maintenance_notice", "kill_switch", "nauta_strict"}
        or (op == "admin" and args.get("do", "") in
            {"", "allow_one", "allow", "remove", "block", "alerts", "pause_queue", "limit", "unlimit"})))
    if not compatible_build(entry) and op not in {"hello", "health", "reinstall"} and not recovery_control:
        reply(None, tr(locale,
            "Esta mini app ya no es válida para esta operación. Si deseas instalar la nueva versión, ve a Ajustes → Sincronización → Pedir app nueva; esta se conserva.",
            "This mini app is no longer valid for this operation. To install the new version, go to Settings → Sync → Get a new app; this one is kept."))
        return
    admin.seen(address)
    if function is None:
        reply(None, tr(locale, "Operación desconocida.", "Unknown operation."))
        return

    def run() -> Any:
        # Re-check when a worker actually starts: revocation can happen after
        # an app request was admitted but before it leaves the executor queue.
        if not access.bound_allowed(entry) or not admin.is_allowed(address) or not _verified_binding(entry):
            raise AppError(tr(locale, "Acceso revocado.", "Access revoked."))
        if not config.service_access_allowed(address):
            raise AppError(tr(locale, "Michiflin está en mantenimiento.", "Michiflin is under maintenance."))
        if not maintenance_safe and state.maintenance_interrupted(generation):
            raise AppError(maintenance_message(locale))
        chat.set_sender(address)
        # Never trust an app-supplied server context scope.
        actual_args = dict(args)
        actual_args.pop("_local_scope", None)
        actual_args.pop("_collab_scope", None)  # never trust a client-supplied project identity
        actual_args.pop("_collab_task", None)
        actual_args.pop("_app_instance_id", None)
        if op == "reinstall":
            actual_args["_app_instance_id"] = instance_id  # verified server context, never from the app
        inline_key: tuple[int, str, str] | None = None
        if op in {"hello", "repair", "models", "ask", "image", "code", "personal_ai"}:
            from . import live_web
            identity = live_web._identity(instance_id)
            secure = bool(identity and identity[1] == address and compatible_build(entry))
            state.set_personal_ai_secure(secure)
            requested = op == "personal_ai" or any(str(args.get(name) or "").startswith("personal-")
                                                   for name in ("space", "model", "agent_model"))
            if requested and not secure:
                raise AppError(tr(locale, "Las APIs personales requieren la app actual en un chat individual cifrado.",
                                  "Personal APIs require the current app in an encrypted one-to-one chat."))
            if (op == "code" and secure and int(identity[0].get("chat") or 0) == chat_id
                    and identity[0].get("epoch") == admitted_entry.get("epoch")
                    and identity[0].get("access_session") == admitted_entry.get("access_session")):
                actual_args["_collab_scope"] = str(instance_id)
            if (op == "ask" and secure and int(identity[0].get("chat") or 0) == chat_id
                    and identity[0].get("epoch") == admitted_entry.get("epoch")
                    and identity[0].get("access_session") == admitted_entry.get("access_session")):
                inline_key = _inline_pages.key(instance_id, address, _inline_scope(admitted_entry, args))
                actual_args["_local_scope"] = f"{instance_id}:{inline_key[2]}"
                actual_args["_collab_scope"] = str(instance_id)
                actual_args["_collab_task"] = rid  # admitted request identity; not from the args payload
        gate = _ai_gate(address) if op in AI_OPS else None
        if gate is not None and not gate.acquire(blocking=False):
            raise AppError(tr(locale, "Espera a que termine tu consulta anterior.", "Wait until your previous request finishes."))
        try:
            if inline_key and not args.get("file") and answer_inline.is_continue(args.get("text")):
                page_status, version = _inline_pages.reserve(inline_key, args.get("history"))
                if page_status != "none" and version is not None:
                    # Do not consume a page here: this worker can outlive its
                    # deadline. Foreground rechecks authorization and version.
                    return _InlineContinuation(inline_key, version)
            if kill_safe:
                answer = function(chat_id, actual_args, locale)
            else:
                with state.guard_work(address, generation):
                    answer = function(chat_id, actual_args, locale)
            if op in AI_OPS and isinstance(answer, dict):
                answer["quota"] = usage.quota_view(address)
            return answer
        finally:
            if gate is not None:
                gate.release()
            chat.stop_capture()

    started = time.monotonic()
    lane = str(request.get("_dispatch_lane") or (
        "owner" if op in {"kill_switch", "owner_only", "maintenance_notice"} or owner_control else "normal"))
    pool = {"owner": _control_workers, "owner_work": _owner_work_workers,
            "pause": _guest_control_workers, "normal": _workers}[lane]
    future = pool.submit(run)
    ticket = str(request.get("_dispatch_ticket") or "")
    if ticket:
        request["_dispatch_worker_started"] = True
        future.add_done_callback(lambda _future: _dispatch_release(ticket))
    timer = threading.Timer(ACK_AFTER, _ack, args=(instance_id, rid), kwargs={"entry": entry})
    timer.daemon = True
    timer.start()
    outcome = "ok"
    try:
        answer = future.result(timeout=OP_LIMITS.get(op, 120))
        if op == "ask":
            # Never mutate continuation state from a worker that can finish
            # after its deadline; recheck the recipient and grant in foreground.
            with state.guard_work(address, generation):
                if isinstance(answer, _InlineContinuation):
                    if not _same_inline_identity(instance_id, admitted_entry):
                        raise AppError(tr(locale, "No pude verificar el acceso a esta respuesta.",
                                          "I could not verify access to this answer."))
                    answer = _inline_pages.take(answer.key, args.get("history"), locale, expected=answer.version)
                    if answer is None:
                        answer = {"text": tr(locale, "Esta parte ya fue atendida; revisa la respuesta anterior.",
                                              "This part was already handled; check the previous answer.")}
                    answer["actions"] = actions_in(str(answer.get("text") or ""), locale)
                    answer["quota"] = usage.quota_view(address)
                elif _same_inline_identity(instance_id, admitted_entry):
                    key = _inline_pages.key(instance_id, address, _inline_scope(admitted_entry, args))
                    answer, paged = _inline_pages.start(key, answer, locale, args.get("text") or "")
                    if paged and isinstance(answer, dict) and "actions" in answer:
                        answer["actions"] = actions_in(str(answer.get("text") or ""), locale)
        reply(answer)
    except FutureTimeout:
        outcome = "timeout"
        reply(None, tr(locale, "La respuesta tardó demasiado; la operación podría seguir en curso. Revisa Descargas antes de repetir.",
                                           "The response took too long; the operation may still be running. Check Downloads before retrying."))
    except state.WorkPaused as exc:
        outcome = "refused: maintenance" if exc.maintenance else "refused: kill switch"
        reply(None, maintenance_message(locale) if exc.maintenance else kill_message(locale))
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
        _local_ai_disclosure(instance_id)


_local_ai_disclosed: dict[int, str] = {}
# A disclosure handoff and its durable marker must not race with another hello
# or a change in collaboration state. This lock is independent of answer work.
_disclosure_lock = threading.Lock()


def _local_ai_disclosure(instance_id: int) -> None:
    """Hand off each relevant revision once per instance where possible.

    The marker is saved only after status handoff and is backed up with state.
    A handoff is not proof that a device displayed it; sending and persisting
    cannot be made atomic, and restoring an older backup may repeat a notice.
    """
    from . import collab_agent, local_ai
    if not local_ai.configured() or maintenance.silent_active():
        return
    with _disclosure_lock:
        collaboration = collab_agent.notice_for_instance(instance_id)
        revision = "collaboration-1" if collaboration else "local-ai-1"
        with state.lock:
            entry = _instance(instance_id)
            already = bool(entry and entry.get("local_ai_disclosure") == revision)
        if already or _local_ai_disclosed.get(instance_id) == revision:
            return
        if collaboration:
            spanish = ("La colaboración autorizada puede consultar APIs, incluidas las personales con posibles cargos, "
                       "y compartir archivos: hasta 16 operaciones externas por proyecto. Para una tarea sin APIs escribe «solo local». "
                       "Para empezar otro proyecto escribe «nuevo proyecto:». El límite se comparte entre el agente y Programador; "
                       "tras una recuperación, lo no usado puede considerarse gastado. Una investigación web cuenta como una operación "
                       "y puede abrir varias páginas. Las búsquedas usan fuentes externas.")
            english = ("Authorized collaboration may use APIs, including personal ones that may incur charges, and share files: "
                       "up to 16 external operations per project. Write ‘local only’ for a task without APIs. Start another project "
                       "with ‘new project:’. The limit is shared by agent and Coder; after recovery, unused calls may be counted as spent. "
                       "One web research operation may open several pages. Searches use external sources.")
        else:
            spanish = "El modo sin cuota puede usar IA local para conversar y programar. Las búsquedas consultan fuentes externas."
            english = "No-quota mode may use local AI for chat and code. Searches use external sources."
        sent = _send_status(instance_id, {"t": "push", "kind": "notice", "data": {
            "kind": "info", "code": revision, "es": spanish, "en": english,
        }})
        if not sent:
            return
        with state.lock:
            entry = _instance(instance_id)
            if entry is not None:
                previous = entry.get("local_ai_disclosure")
                entry["local_ai_disclosure"] = revision
                try:
                    state.save()
                except (OSError, ValueError, TypeError):
                    if previous is None:
                        entry.pop("local_ai_disclosure", None)
                    else:
                        entry["local_ai_disclosure"] = previous
                    log.warning("could not save local AI disclosure marker", exc_info=True)
        # Even if writing the marker failed, avoid repeated pushes until restart.
        if instance_id not in _local_ai_disclosed and len(_local_ai_disclosed) >= 4096:
            _local_ai_disclosed.pop(next(iter(_local_ai_disclosed)))
        _local_ai_disclosed[instance_id] = revision


def replace_app(instance_id: int, chat_id: int, locale: str, address: str = "") -> int:
    """Send an upgrade while keeping the old message and its instance-local storage."""
    from . import live_web
    expected = address.strip().casefold()

    def interrupted() -> bool:
        identity = live_web._identity(instance_id)
        return not identity or identity[1] != expected or int(identity[0].get("chat") or 0) != chat_id

    if interrupted():
        return 0
    new_id = send_app(chat_id, locale, updated=True, address=address, interrupted=interrupted)
    if new_id:
        log.info("app instance %s preserved; new instance %s sent", instance_id, new_id)
    return new_id


def respond(instance_id: int, rid: str, data: Any, error: str = "", *, code: str = "") -> None:
    body = {"t": "res", "re": rid, "ok": not error, "data": data, "error": error}
    if code:
        body["code"] = code
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    size = len(text.encode("utf-8"))
    with _answers_lock:
        item = _answers.setdefault(rid, {"ts": time.time(), "acked": 0.0})
        item.update(state="done", ts=time.time(), body=None, body_size=0)
        if size <= ANSWER_ITEM_BYTES and size <= ANSWER_TOTAL_BYTES:
            used = sum(int(value.get("body_size") or 0) for value in _answers.values())
            oldest = sorted(((key, value) for key, value in _answers.items() if key != rid and value.get("body")),
                            key=lambda pair: float(pair[1].get("ts") or 0))
            for _key, value in oldest:
                if used + size <= ANSWER_TOTAL_BYTES:
                    break
                used -= int(value.get("body_size") or 0)
                value.update(body=None, body_size=0)
            item.update(body=text, body_size=size)
    _deliver(instance_id, rid, text)


def _deliver(instance_id: int, rid: str, text: str) -> None:
    if not _status_allowed(instance_id):
        return
    if len(text.encode("utf-8")) <= CHUNK:
        _send_status(instance_id, json.loads(text))
        return
    # The string is embedded in another JSON object: its characters can take
    # up to four UTF-8 bytes (or require escaping) in that outer update. Keep
    # each complete status update well below WebXDC's 128,000-byte limit.
    width = CHUNK // 4
    pieces = [text[start:start + width] for start in range(0, len(text), width)]
    for index, piece in enumerate(pieces):
        # The recipient, grant or epoch can change between fragments.
        if not _send_status(instance_id, {"t": "chunk", "re": rid, "i": index, "n": len(pieces), "s": piece}):
            break


def sweep_forever() -> None:
    """Re-read pending updates; never send new apps or notices to inactive chats."""
    first, last_profile_check = True, 0.0
    while True:
        try:
            now = time.time()
            if first or now - last_profile_check >= 120:
                notice = maintenance.get()
                if notice["revision"] and not maintenance.silent():
                    threading.Thread(target=maintenance.sync_profile, name="maintenance-profile", daemon=True).start()
                last_profile_check = now
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


def push(chat_id: int, kind: str, data: Any, notify: str = "") -> bool:
    """Queue a push for the newest app; True means accepted locally, not delivered."""
    if maintenance.silent_active():
        return False
    with state.lock:
        found = [(int(key), entry) for key, entry in _apps().items() if int(entry["chat"]) == chat_id
                 and _instance(int(key)) is not None and access.bound_allowed(entry)]
    if not found or not chat.ready():
        return False
    instance_id, entry = max(found, key=lambda item: item[0])
    address = str(entry.get("addr", ""))
    if not address or not access.bound_allowed(entry) or not admin.is_allowed(address) or not _verified_binding(entry):
        return False
    return _send_status(instance_id, {"t": "push", "kind": kind, "data": data}, {address: notify[:120]} if notify and address else None)


def _on_job(job: dict[str, Any], values: dict[str, Any]) -> None:
    """Push queue changes on status transitions, delivered parts and the halfway mark only (each push is an e-mail)."""
    if maintenance.silent_active():
        return
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
        "error": (queue._error_text(job["error"], locale)
                  if (job["status"] == "failed" or job.get("error") == "source_preservation") and job.get("error") else ""),
        "warning": queue.recovery_warning(job, locale),
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
    from . import personal_ai
    providers = []
    registry = ai.providers()
    try:
        if state.request_sender() and state.personal_ai_secure():
            registry += personal_ai.providers(state.request_sender())
    except (personal_ai.PersonalError, OSError):
        pass  # private vault unavailable: never fall back to another person's data
    for item in registry:
        models = []
        for model in item.models:
            remaining = state.remaining_for(model.key)
            models.append({"key": model.key, "title": model.title(), "roles": sorted(model.roles), "used": state.usage_today(model.key),
                           "daily": model.daily, "remaining": list(remaining) if remaining else None,
                           "cooldown": state.cooldown_until(model.key), "reason": state.cooldown_reason(model.key),
                           "health": ai.model_status(model.key), "id": model.model, "accepts": ai.accepts(model), "manual": model.weight <= 0,
                           "local_available": ai.available(model), "personal": item.personal})
        providers.append({"key": item.key, "name": item.name, "daily": item.daily or sum(model.daily for model in item.models),
                          "used": personal_ai.attempts(item.key) if item.personal else sum(state.usage_today(model.key) for model in item.models),
                          "personal": item.personal, "shared_limit": bool(item.daily), "cooldown": state.cooldown_until(item.key),
                          "reason": state.cooldown_reason(item.key), "models": models})
    sources = [{"key": source.key, "label": source.label, "daily": source.daily, "used": state.usage_today(source.key),
                # Personal image and chat requests share the provider's attempt budget.
                "attempts": personal_ai.attempts(source.key.split(":", 1)[0]) if source.personal else None,
                "personal": source.personal, "cooldown": state.cooldown_until(source.key), "reason": state.cooldown_reason(source.key)} for source in images.sources()]
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
        ("video_height", "Calidad de video", "Video quality", "choice", tuple((str(v), f"{v}p", f"{v}p") for v in (144, 240, 360, 480, 720, 1080, 1440, 2160))),
        ("audio_kbps", "Calidad de audio", "Audio quality", "choice", tuple((str(v), f"{v} kbps", f"{v} kbps") for v in (32, 48, 64, 96, 128, 160, 192, 224, 256, 320))),
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
    try:
        weather = weather_home.view(chat.sender(), locale)
    except weather_home.WeatherHomeError:
        weather = None  # do not break the entire app if weather state cannot be saved
    return {"version": __version__, "session": BOOT_SESSION, "admin": admin.is_admin(chat.sender()), "models": models_view(), "health": health_view(),
            "settings": settings_view(locale), "jobs": jobs_view(chat_id, locale), "quota": usage.quota_view(chat.sender()),
            "maintenance_notice": maintenance.get(), "rates_summary": tools.saved_rates_snapshot(), "weather_home": weather,
            "rates_derived_allowed": config.ELTOQUE_DERIVED_CALCULATIONS,
            "modes": {"kill_switch": op_kill_switch(chat_id, {}, locale), "nauta_strict": op_nauta_strict(chat_id, {}, locale)}}


@op("repair")
def op_repair(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Fresh snapshot for an app that cleared its stuck requests."""
    return op_hello(chat_id, args, locale)


@op("reinstall")
def op_reinstall(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """A deliberate request sends one new app; existing instances are untouched."""
    try:
        instance_id = int(args.get("_app_instance_id") or 0)
    except (TypeError, ValueError):
        instance_id = 0
    if instance_id <= 0:
        raise AppError(tr(locale, "No pude verificar la app que solicitó la actualización.",
                          "Could not verify the app requesting the update."))
    address = chat.sender()
    marker = (chat_id, app_build())
    with _upgrade_lock:
        # A double tap must not send two copies. Each later, explicit recovery
        # remains possible after the guard expires.
        if time.time() - _upgraded.get(marker, 0.0) < UPGRADE_GAP:
            return {"sent": True, "already": True}
        new_id = replace_app(instance_id, chat_id, locale, address)
        if not new_id:
            raise AppError(tr(locale, "No pude enviar la app nueva. Inténtalo otra vez.",
                              "Could not send the new app. Please try again."))
        _upgraded[marker] = time.time()
    return {"sent": True}


@op("models")
def op_models(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    return models_view()


@op("ask")
def op_ask(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    raw_text = _clean(args.get("text"))
    text = raw_text[:6000]
    space = str(args.get("space") or "agent")
    attachment = args.get("file") if isinstance(args.get("file"), dict) and space != "agent" else None
    if not text and not attachment:
        raise AppError(tr(locale, "Escribe algo primero.", "Write something first."))
    from . import collab_agent
    local_only = collab_agent.local_only(raw_text)
    if space != "agent" and local_only:
        raise AppError(tr(locale, "Pediste solo local, pero esta opción usa modelos externos; elige Agente local.",
                          "You requested local only, but this option uses external models; choose the local Agent."))
    if space == "agent" and local_only and len(raw_text) > len(text):
        # The opt-out may be beyond the routing limit; never drop it silently.
        raise AppError(tr(locale, "La petición es demasiado larga; no la recorté ni envié. Divídela en partes.",
                          "The request is too long; I did not truncate or send it. Split it into parts."))
    media_parts: list[tuple[str, bytes]] = []
    selected_agent_model = ""
    if attachment:
        selected_agent_model = _clean(args.get("agent_model"))[:180] if space == "smart_agent" else ""
        if selected_agent_model and ai.model_by_key(selected_agent_model) is None:
            raise AppError(tr(locale, "Ese modelo ya no está disponible; elige otro.", "That model is no longer available; pick another."))
        text, media_parts = _attachment(attachment, text, selected_agent_model or ("auto" if space == "smart_agent" else space), locale)
    history = args.get("history") if isinstance(args.get("history"), list) else []
    if space == "agent":
        from . import local_ai
        handled: list[local_ai.Result | None] = []
        scope = str(args.get("_local_scope") or "")[:200]
        from . import collab_agent, coder
        collab_scope = str(args.get("_collab_scope") or "")[:64]
        collab_task = str(args.get("_collab_task") or "")[:40]
        if collab_agent.authorized(collab_scope) and collab_task and collab_agent.general_request(text):
            try:
                answer = collab_agent.run_general(chat_id, text, locale, scope=collab_scope,
                                                  task_id=collab_task, history_scope=scope)
            except coder.CoderError as exc:
                raise AppError(str(exc)) from None
            answer["actions"] = actions_in(answer["text"], locale)
            return answer
        def run_agent() -> None:
            result = (agent.handle(chat_id, text, locale, history, scope=scope) if scope
                      else agent.handle(chat_id, text, locale, history))
            handled.append(result)
        result = run_captured(run_agent, locale)
        generated = handled[0] if handled else None
        if isinstance(generated, local_ai.Result) and generated.status == "ok" and generated.method == "model":
            # Only the trusted route result authorizes this attribution. The
            # installed app already renders `sig` discreetly in its bubble foot.
            footer = "\n\n" + local_ai.model_footer(locale)
            if result["text"].endswith(footer):
                result["text"] = result["text"][:-len(footer)].rstrip()
                result["sig"] = tr(locale, "IA local", "Local AI")
            if generated.truncated:
                result["truncated"] = True
        return result
    if space == "smart_agent" and not attachment:
        agent_model = _clean(args.get("agent_model"))[:180]
        if agent_model and ai.model_by_key(agent_model) is None:
            raise AppError(tr(locale, "Ese modelo ya no está disponible; elige otro.", "That model is no longer available; pick another."))
        try:
            answer = smart_agent.run(text, locale, history, agent_model) if agent_model else smart_agent.run(text, locale, history)
        except smart_agent.AgentError as exc:
            raise AppError(str(exc)) from exc
        answer["models"] = models_view()
        return answer
    messages = [{"role": "system", "content": agent.system_prompt(locale)}]
    budget = 30000
    for turn in reversed(history[-12:]):
        if isinstance(turn, list) and len(turn) == 2 and turn[0] in {"user", "assistant"}:
            content = _clean(turn[1])[:6000]
            budget -= len(content)
            if budget < 0:
                break
            messages.insert(1, {"role": turn[0], "content": content})
    messages.append({"role": "user", "content": text})
    if space == "auto" or (space == "smart_agent" and not selected_agent_model):
        result = ai.complete(messages, agent.pick_role(text), images=media_parts or None)
    elif space == "smart_agent":
        result = ai.complete_exact(messages, selected_agent_model, images=media_parts or None)
    else:
        result = ai.complete_exact(messages, space, images=media_parts or None)
    if not result.ok:
        raise AppError(ai.describe_error(result.error, locale, result, "" if space == "auto" else space).replace("⚠️ IA: ", "").replace("⚠️ ", ""))
    text = result.text
    if result.truncated:
        text += tr(locale, "\n\n[La respuesta se cortó por el límite del proveedor; pide que continúe.]",
                   "\n\n[The answer was cut off by the provider's limit; ask it to continue.]")
    return {"text": text, "sig": result.signature(), "model": result.model, "truncated": result.truncated, "models": models_view()}


@op("personal_ai")
def op_personal_ai(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Manage only this sender's encrypted vault; never return a credential."""
    from . import personal_ai
    if not state.personal_ai_secure():
        raise AppError(tr(locale, "Se requiere un chat individual cifrado.", "An encrypted one-to-one chat is required."))
    action, address = str(args.get("do") or "status"), state.request_sender()
    try:
        if action == "status":
            return {"providers": personal_ai.public_list(address)}
        if action == "handshake":
            return personal_ai.handshake(address)
        if action == "add":
            values = personal_ai.unseal(address, args.get("sealed"))
            return personal_ai.add(address, values)
        provider_id = _clean(args.get("provider"))[:100]
        if action == "refresh":
            return {"providers": personal_ai.refresh(address, provider_id)}
        if action == "remove":
            return {"providers": personal_ai.remove(address, provider_id)}
        if action == "edit":
            return {"providers": personal_ai.edit_provider(address, provider_id, args.get("name"), args.get("daily"))}
        if action == "configure":
            return {"providers": personal_ai.configure_model(address, provider_id, _clean(args.get("model"))[:120],
                                                              args.get("chat"), args.get("vision"), args.get("image"))}
        raise personal_ai.PersonalError("input")
    except (personal_ai.PersonalError, OSError) as exc:
        code = str(exc) if isinstance(exc, personal_ai.PersonalError) else "vault"
        messages = {
            "key_unavailable": ("El servidor no tiene una WEB_SESSION_KEY estable y segura; no se guardó la clave.",
                                "The server lacks a stable, secure WEB_SESSION_KEY; the key was not stored."),
            "vault": ("No se pudo abrir o guardar la bóveda cifrada.", "Could not open or save the encrypted vault."),
            "challenge": ("La autorización de cifrado caducó; vuelve a intentarlo.", "The encryption authorization expired; try again."),
            "envelope": ("No pude verificar el mensaje cifrado; vuelve a intentarlo.", "Could not verify the encrypted message; try again."),
            "url": ("Usa una URL HTTPS pública sin clave, usuario ni parámetros.", "Use a public HTTPS URL without keys, user credentials or parameters."),
            "auth": ("El proveedor rechazó la clave; verifica sus permisos.", "The provider rejected the key; check its permissions."),
            "connection": ("No se pudo conectar de forma segura al proveedor.", "Could not securely connect to the provider."),
            "redirect": ("El proveedor redirigió la solicitud; se bloqueó para proteger la clave.", "The provider redirected the request; it was blocked to protect the key."),
            "quota": ("El proveedor informó un límite o cuota.", "The provider reported a limit or quota."),
            "unsupported": ("Este protocolo o función no está disponible en el servidor elegido.", "This protocol or feature is unavailable on the selected server."),
            "provider_limit": ("Ya hay ocho proveedores personales; elimina uno antes de agregar otro.", "There are already eight personal providers; remove one before adding another."),
            "model_limit": ("Se alcanzó el límite de 200 modelos para este proveedor.", "The 200-model limit for this provider was reached."),
        }
        spanish, english = messages.get(code, ("No pude completar la operación; revisa los datos y el protocolo.",
                                                "Could not complete the operation; check the details and protocol."))
        raise AppError(tr(locale, spanish, english)) from None


@op("browser_session")
def op_browser_session(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Report or erase a saved browser state; never expose its contents to updates."""
    site, action = str(args.get("site") or ""), str(args.get("do") or "status")
    if not web_vault.valid_site(site):
        raise AppError(tr(locale, "Sitio no válido.", "Invalid site."))
    try:
        if action == "status":
            return {"stored": web_vault.load(chat.sender(), site) is not None}
        if action == "disconnect":
            from . import live_web
            return {"removed": live_web.forget_site(chat.sender().strip().casefold(), site)}
    except web_vault.VaultError:
        raise AppError(tr(locale, "No pude acceder a la sesión cifrada del sitio.",
                          "Could not access the site's encrypted session.")) from None
    raise AppError(tr(locale, "Operación desconocida.", "Unknown operation."))


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
    from . import collab_agent, coder, local_ai, local_coder

    raw_text = _clean(args.get("text"))
    text = raw_text[:6000]
    if not text:
        raise AppError(tr(locale, "Describe qué quieres programar.", "Describe what you want to build."))
    incoming = args.get("files") if isinstance(args.get("files"), list) else []
    if len(incoming) > coder.MAX_FILES:
        raise AppError(tr(locale, "El proyecto supera el límite de archivos; no se modificó.",
                          "The project exceeds the file limit; it was not modified."))
    if any(not isinstance(item, dict) for item in incoming):
        raise AppError(tr(locale, "Archivo de proyecto no válido; no se modificó.",
                          "Invalid project file; it was not changed."))
    files = incoming
    model = _clean(args.get("model"))[:180]
    # In a local-AI session the existing Automatic option uses that same
    # engine. An explicitly selected provider keeps its previous behavior.
    # If local startup failed, Automatic fails closed instead of leaking a
    # project to an API. The dedicated key also fails closed if unavailable.
    use_local = model == local_coder.MODEL_KEY or (not model and local_ai.privacy_mode())
    if not use_local and collab_agent.local_only(raw_text):
        raise AppError(tr(locale, "Pediste solo local, pero esta selección usa una API; usa Automático con IA local.",
                          "You requested local only, but this selection uses an API; use Automatic with local AI."))
    scope = str(args.get("_collab_scope") or "")[:64]
    use_collab = bool(use_local and not model and not collab_agent.local_only(raw_text) and collab_agent.authorized(scope))
    if use_local:
        if len(raw_text) > 6000:
            raise AppError(tr(locale, "La petición es demasiado larga para la IA local; no se recortó ni se modificó el proyecto.",
                              "The request is too long for local AI; it was not truncated and the project was not changed."))
        model = collab_agent.MODEL_KEY if use_collab else local_coder.MODEL_KEY
    if model and not use_local and ai.model_by_key(model) is None:
        raise AppError(tr(locale, "Ese modelo ya no está disponible; elige otro o usa Automático.",
                          "That model is no longer available; pick another or use Automatic."))
    address = chat.sender()
    state.charge_once()  # a project is the unit: its many model calls are not counted one by one
    if state.acting_as():
        if not usage.allowed(address, "code"):
            raise AppError(usage.limit_text("code", locale))
        if not use_local and not model.startswith("personal-") and usage.reserve_low():
            raise AppError(usage.reserve_text(locale))
    try:
        owner_scoped = collab_agent.authorized(scope) and (use_collab or (model and not use_local))
        result = (coder.run(text, files, locale, model, scope=scope) if owner_scoped
                  else coder.run(text, files, locale, model))
    except coder.CoderError as exc:
        raise AppError(str(exc))
    if result.get("files") and address and (not use_local or result.get("project_complete", True)):
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
    raw_prompt = _clean(args.get("prompt"))
    prompt = raw_prompt[:1500]
    if not prompt:
        raise AppError(tr(locale, "Describe la imagen.", "Describe the image."))
    from . import collab_agent
    if collab_agent.local_only(raw_prompt):
        raise AppError(tr(locale, "Pediste solo local y no hay un generador local de imágenes; no envié la petición.",
                          "You requested local only and there is no local image generator; the request was not sent."))
    model = _clean(args.get("model"))[:180]
    result = images.generate(prompt, model)
    if result.error == "person_quota":
        raise AppError(usage.limit_text("image", locale))
    if not result.data:
        name = result.source or tr(locale, "El modelo", "The model")
        back = _clock(state.cooldown_until(model)) if model else ""
        reason = {"no_source": tr(locale, "No hay generadores configurados.", "No generators are configured."),
                  "unknown": tr(locale, "Ese modelo ya no está disponible. Elige otro.", "That model is no longer available. Pick another one."),
                  "cooldown": tr(locale, f"{name} está descansando. Elige otro modelo o Automático.", f"{name} is resting. Pick another model or Automatic."),
                  "quota": tr(locale, f"{name}: alcanzó un límite local o del proveedor{f'; prueba después de las {back}' if back else ''}. Elige otro modelo o Automático."
                              if model else "No quedan generadores dentro de los límites locales o del proveedor.",
                              f"{name}: a local or provider limit was reached{f'; try after {back}' if back else ''}. Pick another model or Automatic."
                              if model else "No generators remain within local or provider limits."),
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


@op("youtube_search")
def op_youtube_search(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    query = _clean(args.get("q"))[:300]
    page = args.get("page", 1)
    if not query or type(page) is not int or not 1 <= page <= tools.VIDEO_MAX_PAGES:
        raise AppError(tr(locale, "Consulta o página no válida.", "Invalid query or page."))
    try:
        found = tools.youtube_page(query, locale, page, chat.sender())
    except ValueError:
        raise AppError(tr(locale, "No pude buscar esa página.", "Could not search that page.")) from None
    return {"q": query, "page": page, "more": found["more"], "limited": found["limited"],
            "results": [{"title": item.title, "url": item.url, "snippet": item.snippet,
                         "source": item.source, "date": item.date} for item in found["results"]]}


@op("music_search")
def op_music_search(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    query = _clean(args.get("q"))[:200]
    if not query:
        raise AppError(tr(locale, "Escribe qué música buscas.", "Enter the music you want to find."))
    try:
        return {"q": query, "results": music.search(query)}
    except music.MusicError as exc:
        raise AppError(tr(locale, "La biblioteca no respondió. Prueba otra vez.",
                          "The library did not respond. Try again.")) from exc


@op("music_item")
def op_music_item(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    try:
        return music.item(_clean(args.get("id")))
    except music.MusicError as exc:
        raise AppError(tr(locale, "No pude leer esta colección de audio.",
                          "Could not read this audio collection.")) from exc


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
    if quality and ((kind == "audio" and quality not in media.AUDIO_RATES) or
                    (kind == "video" and quality not in media.VIDEO_HEIGHTS)):
        raise AppError(tr(locale, "Calidad no válida.", "Invalid quality."))
    audio_format = str(args.get("audio_format") or "").lower()
    video_format = str(args.get("video_format") or "").lower()
    if ((audio_format and (kind != "audio" or audio_format not in {"mp3", "m4a", "opus"})) or
            (video_format and (kind != "video" or video_format not in {"mp4", "mkv", "webm"}))):
        raise AppError(tr(locale, "Formato no válido.", "Invalid format."))
    if kind == "audio" and audio_format:
        operation = lambda: web.enqueue_media(chat_id, url, kind, quality or int(state.setting("audio_kbps")), locale, audio_format)
    elif kind == "video" and video_format:
        operation = lambda: web.enqueue_media(chat_id, url, kind, quality or int(state.setting("video_height")), locale, "", video_format)
    else:
        operation = lambda: ACTIONS[kind](chat_id, url, locale, quality)
    return run_captured(operation, locale) | {"jobs": jobs_view(chat_id, locale)}


@op("command")
def op_command(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    """Run a tap command from a reply; reader links open inside the app."""
    raw_text = _clean(args.get("text"))
    from . import collab_agent
    if collab_agent.local_only(raw_text):
        raise AppError(tr(locale, "Pediste solo local. No ejecuté este comando, que podría usar Internet; usa el Agente local.",
                          "You requested local only. I did not run this command, which could use the Internet; use the local Agent."))
    text = raw_text[:2000]
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
    history = args.get("history") is True
    try:
        if history:
            return tools.historical_rates(args.get("days", 7))
        snapshot = tools.rates_snapshot()
    except tools.RateError as exc:
        reason = str(exc)
        if reason == "authorization":
            raise AppError(tr(locale, "La API de elTOQUE requiere una clave válida de esta aplicación.",
                              "The elTOQUE API requires a valid key for this application.")) from None
        if reason == "rate_limit":
            raise AppError(tr(locale, "elTOQUE limitó las consultas. Espera y vuelve a intentarlo; no se sustituyen sus datos.",
                              "elTOQUE limited requests. Wait before trying again; no substitute data are used.")) from None
        raise AppError(tr(locale, "No pude consultar elTOQUE ahora. Inténtalo más tarde.",
                          "Could not retrieve elTOQUE rates right now. Try again later.")) from None
    except state.WorkPaused:
        raise
    except Exception:
        raise AppError(tr(locale, "No pude consultar elTOQUE ahora. Inténtalo más tarde.",
                          "Could not retrieve elTOQUE rates right now. Try again later.")) from None
    return {**snapshot, "units": tools.rate_units(snapshot["rates"]), "monitor": rate_store.status(),
            "refresh_seconds": config.ELTOQUE_REFRESH_SECONDS,
            "derived_allowed": config.ELTOQUE_DERIVED_CALCULATIONS}


@op("weather")
def op_weather(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    try:
        place_id = args.get("place_id", 0)
        if type(place_id) is not int or place_id < 0:
            raise tools.WeatherError("place_changed")
        return weather_home.query(chat.sender(), _clean(args.get("city"))[:120], locale, place_id)
    except tools.WeatherError as exc:
        if str(exc) == "not_found":
            raise AppError(tr(locale, "No encontré esa ciudad. Prueba con otro nombre.", "City not found. Try another name.")) from None
        raise AppError(tr(locale, "No pude consultar Open-Meteo. Inténtalo de nuevo.", "Could not reach Open-Meteo. Try again.")) from None


@op("weather_settings")
def op_weather_settings(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    action = str(args.get("do") or "")
    try:
        value = args.get("on") if action == "auto" else {"city": _clean(args.get("city"))[:120], "place_id": args.get("place_id")}
        weather_home.settings(chat.sender(), action, value)
        return weather_home.view(chat.sender(), locale)
    except weather_home.WeatherHomeError as exc:
        if str(exc) == "no_result":
            raise AppError(tr(locale, "Consulta y guarda primero una ciudad.", "First look up and save a city.")) from None
        if str(exc) == "changed":
            raise AppError(tr(locale, "La última consulta cambió. Vuelve a abrir o consultar la ciudad antes de elegirla.",
                              "The last lookup changed. Reopen or look up the city before selecting it.")) from None
        if str(exc) in {"storage", "full"}:
            raise AppError(tr(locale, "No pude guardar el ajuste del clima.", "Could not save the weather setting.")) from None
        raise AppError(tr(locale, "No se pudo cambiar ese ajuste.", "Could not change that setting.")) from None


@op("translate")
def op_translate(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    text = _clean(args.get("text"))
    if not text:
        return {"text": "", "detected": "", "provider": ""}
    if len(text) > 4500:
        raise AppError(tr(locale, "El texto supera 4500 caracteres; divídelo antes de traducir.",
                          "Text exceeds 4500 characters; split it before translating."))
    target = str(args.get("target") or ("en" if locale == "es" else "es"))
    source = str(args.get("source") or "auto")
    if target not in extras.LANGUAGES or (source != "auto" and source not in extras.LANGUAGES):
        raise AppError(tr(locale, "Idioma no disponible.", "Language unavailable."))
    result = extras.translate_detailed(text, target, source)
    if not result["text"]:
        messages = {
            "detection_uncertain": ("No pude identificar el idioma con suficiente seguridad. Elige el idioma de origen e inténtalo otra vez.",
                                    "Could not identify the language confidently. Select the source language and try again."),
            "pair_unavailable": ("Apertium no dispone de ese par de idiomas y los demás servicios no respondieron. Prueba más tarde.",
                                 "Apertium does not support this language pair and the other services did not answer. Try later."),
            "rate_limited": ("Los servicios están limitando las solicitudes o no responden. Espera unos minutos e inténtalo de nuevo.",
                             "Services are rate-limiting requests or not responding. Wait a few minutes and try again."),
        }
        es, en = messages.get(result.get("error", ""),
                              ("Los traductores no respondieron. Si usabas detección automática, elige el idioma de origen e inténtalo otra vez.",
                               "Translation services did not answer. If using detection, choose the source language and try again."))
        raise AppError(tr(locale, es, en))
    return result


@op("filekit")
def op_filekit(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    try:
        return filekit_api.dispatch(chat_id, args, locale)
    except filekit.FileKitError as exc:
        raise AppError(filekit.error_text(str(exc), locale)) from None
    except filekit_api.DeliveryRefused as exc:
        raise AppError(str(exc)) from None


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
    if isinstance(value, (bool, str)):
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
    if not admin.is_admin(chat.sender()):
        raise AppError(tr(locale, "Solo los administradores autorizados pueden cambiar el acceso.", "Only authorized administrators can change access."))
    if "enabled" in args:
        enabled, minutes = args.get("enabled"), args.get("minutes", 60)
        if type(enabled) is not bool or not access.valid_duration(minutes):
            raise AppError(tr(locale, "Modo o duración inválidos.", "Invalid mode or duration."))
        if not access.set_owner_only(enabled, minutes, str(args.get("nonce") or "")):
            raise AppError(tr(locale, "No pude confirmar el cambio o su respaldo. Actualiza Administración y comprueba el estado antes de reiniciar.",
                              "The change or its backup could not be confirmed. Refresh Administration and check the status before restarting."))
        queue.wake()
    return admin.snapshot() | {"usage": usage.report(), "perf": perf.snapshot(), "maintenance_notice": maintenance.get(), "message": ""}



@op("maintenance_notice")
def op_maintenance_notice(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    if not admin.is_admin(chat.sender()):
        raise AppError(tr(locale, "Solo los administradores autorizados pueden editar este aviso.", "Only authorized administrators can edit this notice."))
    if "active" not in args:
        return {"notice": maintenance.get(), "profile_updated": None}
    try:
        notice = maintenance.set_notice(args.get("active"), args.get("es"), args.get("en"), args.get("minutes"))
    except ValueError:
        raise AppError(tr(locale, "Aviso inválido: usa un mensaje de hasta 280 caracteres.",
                          "Invalid notice: use a message of up to 280 characters.")) from None
    profile_updated = maintenance.sync_profile()
    return {"notice": notice, "profile_updated": profile_updated}


@op("admin")
def op_admin(chat_id: int, args: dict[str, Any], locale: str) -> Any:
    if not admin.is_admin(chat.sender()):
        raise AppError(tr(locale, "Solo para administradores.", "Administrators only."))
    action, address = str(args.get("do") or ""), _clean(args.get("address")).lower()
    message = ""
    if action in {"allow_one", "allow", "remove", "block"}:
        if action == "allow_one":
            if not config.is_owner_admin(chat.sender()):
                raise AppError(tr(locale, "Solo los administradores autorizados pueden autorizar a un invitado.", "Only authorized administrators can approve a guest."))
            done = admin.grant_one(address, args.get("minutes", 60), str(args.get("nonce") or ""))
        else:
            try:
                done = admin.grant(address) if action == "allow" else admin.revoke(address, block=action == "block")
            except Exception as exc:
                from . import community
                if not isinstance(exc, community.CheckpointError):
                    raise
                raise AppError(tr(locale, "El cambio local no pudo respaldarse. Repite la operación antes de reiniciar.",
                                  "The local change could not be backed up. Retry the operation before restarting.")) from None
        queue.wake()
        message = tr(locale, "Desbloqueado. Debe completar de nuevo la invitación privada.",
                     "Unblocked. They must complete the private invitation again.") if done and action == "allow" and config.INVITE_AUTO_ACCESS else tr(locale, "Hecho.", "Done.") if done else tr(locale,
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
    return admin.snapshot() | {"usage": usage.report(), "perf": perf.snapshot(), "maintenance_notice": maintenance.get(), "message": message.replace("🔄 ", "").replace("⚠️ ", "")}

