"""Access control and owner tools: allowlist, contact attempts, alerts, recent log lines, restart."""
from __future__ import annotations

import collections
import ipaddress
import logging
import os
import re
import secrets
import threading
import time
import traceback
from typing import Any

import requests

from . import access, chat, config, names, state
from .util import local_time, tr

log = logging.getLogger("michiflin")
_recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=120)
_recent_lock = threading.RLock()
_activity: dict[str, float] = {}
ALERT_GAP = 6 * 3600
RECENT_ATTEMPT_SECONDS = 24 * 3600


def redact_log(value: str) -> str:
    """Best-effort redaction before diagnostics reach the owner's app or server logs."""
    text = str(value)
    # Strip URLs entirely: paths, fragments and query strings can all carry secrets.
    text = re.sub(r"(?i)\bhttps?://[^\s<>\"']+", "[url]", text)
    text = re.sub(r"(?i)\bOPENPGP4FPR:[^\s<>]+", "[invitation]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[address]", text)
    text = re.sub(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", "[ip]", text)
    def mask_ipv6(match: re.Match[str]) -> str:
        try:
            ipaddress.IPv6Address(match.group())
        except ValueError:
            return match.group()  # e.g. file.py:163: or a timestamp
        return "[ip]"
    text = re.sub(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])", mask_ipv6, text)
    text = re.sub(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})", "[credential]", text)
    text = re.sub(r"(?im)\b(cookie|set-cookie|authorization|proxy-authorization)\s*[:=][^\r\n]*", r"\1: [credential]", text)
    text = re.sub(r"(?i)\b(bearer|basic)\s+[^\s,;]+", r"\1 [credential]", text)
    text = re.sub(r"(?i)([\"']?(?:authorization|proxy-authorization|cookie|set-cookie|password|passphrase|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+", r"\1[credential]", text)
    return text


def _log_line(entry: dict[str, Any]) -> str:
    when = local_time(float(entry["last"]), "%d/%m %H:%M:%S")
    count = f" ×{entry['count']}" if entry["count"] > 1 else ""
    return f"{when} {entry['level']}{count} {entry['message']}"


class _Buffer(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            message = redact_log(record.getMessage())[:4000]
            detail = redact_log("".join(traceback.format_exception(*record.exc_info)))[:8000] if record.exc_info else ""
            entry = {"first": record.created, "last": record.created, "level": record.levelname,
                     "logger": record.name, "message": message, "detail": detail, "count": 1}
            with _recent_lock:
                previous = _recent[-1] if _recent else None
                if previous and all(previous[key] == entry[key] for key in ("level", "logger", "message", "detail")):
                    previous["last"] = record.created
                    previous["count"] += 1
                else:
                    _recent.append(entry)
        except Exception:
            # Logging must never expose an unredacted record if sanitization fails.
            return


def install_log_buffer() -> None:
    logger = logging.getLogger("michiflin")
    if not any(isinstance(handler, _Buffer) for handler in logger.handlers):
        logger.addHandler(_Buffer())


def clear_logs() -> None:
    """Clear only the in-memory diagnostics for this session, not state or server logs."""
    with _recent_lock:
        _recent.clear()


def _access() -> dict[str, Any]:
    with state.lock:
        access = state.data.setdefault("access", {})
        access.setdefault("allowed", [])
        access.setdefault("blocked", [])
        access.setdefault("attempts", {})
        access.setdefault("alerts", True)
        return access


def is_admin(address: str) -> bool:
    address = (address or "").strip().lower()
    return config.is_owner_admin(address) if config.OWNER_ADDRESS else address in config.ADMIN_ADDRESSES


def is_allowed(address: str) -> bool:
    address = (address or "").strip().lower()
    if not config.service_access_allowed(address):
        return False
    if is_admin(address):
        return True
    if access.snapshot()["controlled"]:
        return address not in _access()["blocked"]  # service gate already requires an explicit current grant
    return address in config.ALLOWED_SENDERS or address in _access()["allowed"]


def is_blocked(address: str) -> bool:
    return address.lower() in _access()["blocked"]


def seen(address: str) -> None:
    _activity[address.lower()] = time.time()


def record_attempt(address: str, text: str) -> bool:
    """Log a message from an unknown contact; True when the admins should be alerted now."""
    address = address.lower() or "?"
    now = time.time()
    with state.lock:
        attempts = _access()["attempts"]
        entry = attempts.setdefault(address, {"count": 0, "first": now, "last": 0.0, "alerted": 0.0, "sample": ""})
        entry["count"] += 1
        entry["last"] = now
        # Do not retain new message text: it may contain passwords or other private data.
        # Existing samples in historical state are kept untouched but are not displayed.
        alert = bool(_access()["alerts"]) and now - float(entry.get("alerted") or 0) > ALERT_GAP
        if alert:
            entry["alerted"] = now
        if len(attempts) > 200 and not access.preserve_history():
            for stale in sorted(attempts, key=lambda key: attempts[key]["last"])[:50]:
                attempts.pop(stale, None)
    state.save()
    return alert


def alert_admins(message: str) -> None:
    """Send a notice to every admin chat the bot already knows."""
    for chat_id in admin_chats():
        chat.text(chat_id, message)


def alert_text(address: str, text: str) -> str:
    locale = "en" if state.setting("language") == "en" else "es"
    if access.snapshot()["private_mode"]:
        return tr(locale, f"🛡️ Contacto ajeno bloqueado: {address}\nModo privado: solo pueden entrar el dueño y el administrador de respaldo configurado.",
                  f"🛡️ Unknown contact blocked: {address}\nPrivate mode: only the owner and configured backup administrator can enter.")
    allow = names.cmd(locale, "admin", "allow")
    return tr(locale, f"🛡️ Contacto ajeno bloqueado: {address}\nPara darle acceso: {allow} {address}",
              f"🛡️ Unknown contact blocked: {address}\nTo grant access: {allow} {address}")


def block_unknown(address: str, contact_id: int = 0) -> None:
    address = address.lower()
    if not address or is_allowed(address):
        return
    with state.lock:
        blocked = _access()["blocked"]
        if address not in blocked:
            blocked.append(address)
    _set_blocked_contact(address, True, contact_id)
    state.save()


def admin_chats() -> list[int]:
    found = []
    if not chat.ready():
        return found
    for address in config.ADMIN_ADDRESSES:
        if not is_admin(address) or not config.service_access_allowed(address):
            continue
        try:
            from . import identity_guard
            chat_id = identity_guard.current_email_chat(address)
            if chat_id:
                members = chat.rpc().get_chat_contacts(chat.account(), chat_id)
                contact = int(members[0]) if len(members) == 1 else 0
            else:
                contact = chat.rpc().lookup_contact_id_by_addr(chat.account(), address)
                chat_id = chat.rpc().get_chat_id_by_contact_id(chat.account(), contact) if contact else 0
            if chat_id and contact and identity_guard.authorized_chat(chat.rpc(), chat.account(), int(chat_id),
                                                                      address, contact_id=int(contact)):
                found.append(int(chat_id))
        except Exception:
            continue
    return found


def grant_one(address: str, minutes: int, nonce: str) -> bool:
    """From owner-only mode, temporarily authorize exactly one named contact."""
    address = (address or "").strip().lower()
    mode = access.snapshot()
    if (not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,255}", address) or config.is_owner_admin(address)
            or type(minutes) is not int or minutes not in access.DURATIONS or state.maintenance_active()
            or mode["maintenance"] or not mode["owner_only"]
            or not secrets.compare_digest(str(nonce), mode["nonce"])):
        return False
    # Do not admit anyone merely by unblocking a Delta Chat contact. The
    # nonce-checked access transition below is still mandatory.
    if not _set_blocked_contact(address, False):
        return False
    revision = access.open_one_guest(address, minutes, nonce)
    if revision is None:
        if is_blocked(address):
            _set_blocked_contact(address, True)
        return False
    was_blocked = False
    try:
        with state.lock:
            blocked = _access()["blocked"]
            was_blocked = address in blocked
            if was_blocked:
                blocked.remove(address)
        state.save()
    except Exception:
        access.rollback_one_guest(address, revision)
        with state.lock:
            if was_blocked and address not in _access()["blocked"]:
                _access()["blocked"].append(address)
        if was_blocked:
            _set_blocked_contact(address, True)
        raise
    return True


def grant(address: str) -> bool:
    address = address.strip().lower()
    if not re.fullmatch(r"[^\s@]{1,128}@[^\s@]{1,255}", address):
        return False
    mode = access.snapshot()
    if mode["private_mode"] and not config.is_owner_admin(address):
        return False
    if mode["controlled"] and not config.is_owner_admin(address):
        # A core-level block prevents delivery even when our allowlist says yes.
        if not _set_blocked_contact(address, False) or not access.allow_guest(address, mode["revision"]):
            return False
        was_blocked = False
        try:
            with state.lock:
                blocked = _access()["blocked"]
                was_blocked = address in blocked
                if was_blocked:
                    blocked.remove(address)
            state.save()
        except Exception:
            access.revoke_guest(address)
            with state.lock:
                if was_blocked and address not in _access()["blocked"]:
                    _access()["blocked"].append(address)
            raise
        return True
    with state.lock:
        data = _access()
        if address not in data["allowed"]:
            data["allowed"].append(address)
        if address in data["blocked"]:
            data["blocked"].remove(address)
        data["attempts"].pop(address, None)
    _set_blocked_contact(address, False)
    state.save()
    return True


def revoke(address: str, block: bool = False) -> bool:
    address = address.strip().lower()
    if "@" not in address or is_admin(address):
        return False
    mode = access.snapshot()
    if mode["controlled"] or mode["private_mode"]:
        # Revocation closes live access without deleting settings, jobs or history.
        if not block and not mode["controlled"]:
            return False
        if mode["controlled"]:
            access.revoke_guest(address)
        if block:
            with state.lock:
                data = _access()
                if address not in data["blocked"]:
                    data["blocked"].append(address)
            _set_blocked_contact(address, True)
            state.save()
        return True
    with state.lock:
        data = _access()
        if address in data["allowed"]:
            data["allowed"].remove(address)
        if block and address not in data["blocked"]:
            data["blocked"].append(address)
        # A previously delivered WebXDC remains usable unless its server-side
        # instance is forgotten as well as its allow-list entry.
        apps = state.data.get("apps", {})
        for key in [key for key, item in apps.items() if str(item.get("addr", "")).lower() == address]:
            apps.pop(key, None)
    state.forget_person(address)
    if block:
        _set_blocked_contact(address, True)
    state.save()
    return True


def set_alerts(enabled: bool) -> None:
    with state.lock:
        _access()["alerts"] = enabled
    state.save()


def _set_blocked_contact(address: str, blocked: bool, contact_id: int = 0) -> bool:
    """Change the core block; False means an unblock could not be verified."""
    if not chat.ready():
        return False
    try:
        rpc, account = chat.rpc(), chat.account()
        if blocked:
            contact_id = contact_id or int(rpc.lookup_contact_id_by_addr(account, address) or 0)
            if contact_id:
                rpc.block_contact(account, contact_id)
            return True
        for contact in rpc.get_blocked_contacts(account):
            if str(contact.address).lower() == address:
                rpc.unblock_contact(account, contact.id)
        return True
    except Exception as exc:
        log.warning("contact block change failed: %s", exc)
        return False


def overview(locale: str) -> str:
    data, mode = _access(), access.snapshot()
    now = time.time()
    private = mode["private_mode"]
    users = (list(config.owner_admin_addresses()) + mode["guests"] if mode["controlled"]
             else sorted(config.owner_admin_addresses() | config.ALLOWED_SENDERS | set(data["allowed"])))
    users = sorted(set(address for address in users if address and is_allowed(address)))
    lines = [tr(locale, "🛡️ Administración", "🛡️ Administration"), "",
             tr(locale, f"OWNER_ONLY={'1' if mode['owner_only'] else '0'} · "
                + ("mantenimiento activo" if mode['maintenance'] else "sesión actual"),
                f"OWNER_ONLY={'1' if mode['owner_only'] else '0'} · "
                + ("maintenance active" if mode['maintenance'] else "current session"))]
    if mode["expires"]:
        lines.append(tr(locale, f"Vuelve a privado: {local_time(mode['expires'], '%d/%m %H:%M')}",
                        f"Returns to private: {local_time(mode['expires'], '%d/%m %H:%M')}"))
    lines += ["", tr(locale, "Acceso privado del dueño:", "Owner's private access:") if private
              else tr(locale, "Usuarios con acceso:", "Users with access:")]
    for address in users:
        role = tr(locale, "admin", "admin") if is_admin(address) else tr(locale, "usuario", "user")
        last = _activity.get(address)
        when = tr(locale, f"activo hace {_ago(now - last, locale)}", f"active {_ago(now - last, locale)} ago") if last else tr(locale, "sin actividad en esta sesión", "no activity this session")
        lines.append(f"• {_mask(address)} · {role} · {when}")
    recent_only = private or mode["controlled"]
    attempts = {address: entry for address, entry in data["attempts"].items()
                if not recent_only or now - float(entry.get("last") or 0) <= RECENT_ATTEMPT_SECONDS}
    lines += ["", tr(locale, f"Intentos de contacto ajenos: {len(attempts)}", f"Foreign contact attempts: {len(attempts)}")]
    for address, entry in sorted(attempts.items(), key=lambda item: -item[1]["last"])[:8]:
        lines.append(f"• {address} · {entry['count']}× · {local_time(entry['last'], '%d/%m %H:%M')}")
    lines += ["", tr(locale, f"Alertas de contactos ajenos: {'sí' if data['alerts'] else 'no'}", f"Foreign contact alerts: {'yes' if data['alerts'] else 'no'}")]
    run = os.getenv("GITHUB_RUN_ID", "")
    if run:
        lines.append(tr(locale, f"Sesión del servidor: {run}", f"Server session: {run}"))
    return "\n".join(lines)


def snapshot() -> dict[str, Any]:
    """Structured administration view; hidden history remains untouched in state."""
    data, mode = _access(), access.snapshot()
    private = mode["private_mode"]
    users = (list(config.owner_admin_addresses()) + mode["guests"] if mode["controlled"]
             else sorted(config.owner_admin_addresses() | config.ALLOWED_SENDERS | set(data["allowed"])))
    users = sorted(set(address for address in users if address and is_allowed(address)))
    now = time.time()
    recent_only = private or mode["controlled"]
    attempts = [(address, entry) for address, entry in data["attempts"].items()
                if not recent_only or now - float(entry.get("last") or 0) <= RECENT_ATTEMPT_SECONDS]
    recent_addresses = {address for address, _entry in attempts}
    blocked = [] if private else [address for address in data["blocked"] if not mode["controlled"] or address in recent_addresses]
    with _recent_lock:
        entries: list[dict[str, Any]] = []
        budget = 60_000
        for entry in reversed(_recent):
            size = len(entry["message"]) + len(entry["detail"])
            if size > budget:
                break
            entries.insert(0, dict(entry))
            budget -= size
            if len(entries) == 60:
                break
    return {
        "private_mode": private, "owner_only": mode["owner_only"], "maintenance": mode["maintenance"],
        "owner_only_expires": mode["expires"], "server_now": now, "session_control": mode["controlled"],
        "control_nonce": mode["nonce"], "preserve_history": access.preserve_history(),
        "email_key_recovery": bool(config.EMAIL_KEY_RECOVERY),
        "users": [{"address": address, "admin": is_admin(address), "last": _activity.get(address, 0.0),
                   "active": is_allowed(address)} for address in users],
        "attempts": [{"address": address, "count": entry["count"], "last": entry["last"]}
                     for address, entry in sorted(attempts, key=lambda item: -item[1]["last"])[:30]],
        "blocked": blocked, "alerts": bool(data["alerts"]),
        "logs": [_log_line(entry)[:500] for entry in entries[-40:]], "log_entries": list(reversed(entries)),
        "session": os.getenv("GITHUB_RUN_ID", ""),
    }


def recent_logs(locale: str) -> str:
    with _recent_lock:
        entries = [dict(entry) for entry in list(_recent)[-25:]]
    if not entries:
        return tr(locale, "📜 Sin advertencias ni errores en esta sesión.", "📜 No warnings or errors this session.")
    return tr(locale, "📜 Últimas advertencias y errores", "📜 Latest warnings and errors") + "\n\n" + "\n".join(_log_line(entry) for entry in entries)


def restart(locale: str) -> str:
    """Queue a fresh server session, then stop this one after the state is saved."""
    repository, token = os.getenv("GITHUB_REPOSITORY", ""), config.MICHIFLIN_GITHUB_TOKEN
    if not repository or not token:
        return tr(locale, "⚠️ El reinicio remoto necesita el token de GitHub del bot.", "⚠️ Remote restart needs the bot's GitHub token.")
    try:
        response = requests.post(f"https://api.github.com/repos/{repository}/actions/workflows/michiflin.yml/dispatches",
                                 json={"ref": os.getenv("GITHUB_REF_NAME", "main")}, timeout=20,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    except requests.RequestException:
        return tr(locale, "⚠️ No pude contactar con GitHub.", "⚠️ Could not reach GitHub.")
    if response.status_code != 204:
        return tr(locale, f"⚠️ GitHub rechazó el reinicio ({response.status_code}).", f"⚠️ GitHub refused the restart ({response.status_code}).")

    def stop() -> None:
        time.sleep(20)
        state.save()
        os._exit(0)

    threading.Thread(target=stop, name="restart", daemon=True).start()
    return tr(locale, "🔄 Reiniciando: vuelvo en unos 3 minutos.", "🔄 Restarting: back in about 3 minutes.")


def _mask(address: str) -> str:
    user, _, domain = address.partition("@")
    return f"{user[:3]}…@{domain}" if len(user) > 3 else address


def _ago(seconds: float, locale: str) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"
