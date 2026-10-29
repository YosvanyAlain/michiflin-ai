"""Cooperative, shared elTOQUE polling and private owner anomaly notices."""
from __future__ import annotations

from datetime import datetime, timedelta
import threading
import time
from typing import Any

from . import config, rate_store, state, tools

ALERT_INTERVAL = 6 * 3600
SAFE_ERRORS = {"authorization", "rate_limit", "connection", "timeout", "http", "invalid", "empty", "storage"}


def _current_health() -> dict[str, Any]:
    with state.lock:
        section = state.data.setdefault("eltoque", {})
        value = section.setdefault("health", {})
        return dict(value) if isinstance(value, dict) else {}


def _owner_notification(es: str, en: str, kind: str) -> bool:
    """Deliver only to an existing verified owner chat; never to an arbitrary address match."""
    from . import chat, identity_guard, miniapp
    if not chat.ready() or not config.OWNER_ADDRESS:
        return False
    with state.lock:
        entries = [dict(item) for item in state.data.get("apps", {}).values()
                   if isinstance(item, dict) and str(item.get("addr", "")).strip().casefold() == config.OWNER_ADDRESS.casefold()]
    for entry in sorted(entries, key=lambda item: float(item.get("sent") or 0), reverse=True):
        try:
            chat_id = int(entry["chat"])
            rpc, account = chat.rpc(), chat.account()
            info = rpc.get_basic_chat_info(account, chat_id)
            members = rpc.get_chat_contacts(account, chat_id)
            kind_value = getattr(info.chat_type, "value", info.chat_type)
            if (str(kind_value) != "Single" or not info.is_encrypted or info.is_contact_request or info.is_self_talk
                    or len(members) != 1):
                continue
            contact = rpc.get_contact(account, int(members[0]))
            if (str(contact.address or "").strip().casefold() != config.OWNER_ADDRESS.casefold()
                    or not contact.is_verified or contact.verifier_id != 1
                    or not contact.is_key_contact or not contact.e2ee_avail):
                continue
            if not identity_guard.authorized_entry(rpc, account, entry):
                continue
            locale = "en" if state.setting("language") == "en" else "es"
            return bool(miniapp.push(chat_id, "notice", {"es": es, "en": en, "kind": kind}, en if locale == "en" else es))
        except Exception:
            continue
    return False


def _description(reason: str) -> tuple[str, str]:
    descriptions = {
        "authorization": ("la clave no fue aceptada", "the key was rejected"),
        "rate_limit": ("el proveedor limitó las consultas", "the provider rate-limited requests"),
        "connection": ("no hay conexión con el proveedor", "the provider could not be reached"),
        "timeout": ("la consulta agotó el tiempo", "the request timed out"),
        "http": ("el proveedor devolvió un error", "the provider returned an error"),
        "invalid": ("la respuesta no tiene el formato esperado", "the response has an unexpected format"),
        "empty": ("no llegaron tasas utilizables", "no usable rates were returned"),
        "storage": ("no se pudo guardar la consulta", "the retrieved data could not be saved"),
    }
    return descriptions.get(reason, descriptions["http"])


def record_health(error: str = "", now: float | None = None) -> None:
    """Record bounded, sanitized state and retry undelivered owner notices."""
    now = time.time() if now is None else now
    reason = error if error in SAFE_ERRORS else ("http" if error else "")
    with state.lock:
        section = state.data.setdefault("eltoque", {})
        health = section.setdefault("health", {})
        previous = str(health.get("error") or "")
        if reason:
            if not previous:
                health["since"] = now
            health["error"] = reason
            health["failures"] = int(health.get("failures") or 0) + 1
            if reason != previous or now - float(health.get("alerted_at") or 0) >= ALERT_INTERVAL:
                health["pending"] = "error"
        else:
            health["error"] = ""
            health["failures"] = 0
            health["ok_at"] = now
            if previous and (health.get("alerted_at") or health.get("pending") == "error"):
                # If the owner was offline during the incident, retain a brief
                # recovery notice rather than silently losing the anomaly.
                health["pending"] = "recovered"
            elif previous:
                health.pop("pending", None)
        pending = health.get("pending")
        # A failed write must not prevent an owner warning; no secrets are stored here.
        try:
            state.save()
        except (OSError, TypeError, ValueError):
            pass
    if pending == "error":
        detail_es, detail_en = _description(reason or previous)
        sent = _owner_notification(f"⚠️ elTOQUE: {detail_es}. Se conservan las últimas tasas recibidas, marcadas con su fecha.",
                                   f"⚠️ elTOQUE: {detail_en}. The last received rates are retained with their timestamp.", "warn")
    elif pending == "recovered":
        sent = _owner_notification("✅ La consulta de elTOQUE volvió a funcionar.", "✅ elTOQUE requests are working again.", "ok")
    else:
        return
    if sent:
        with state.lock:
            health = state.data.setdefault("eltoque", {}).setdefault("health", {})
            if health.get("pending") == pending:
                health.pop("pending", None)
                health["alerted_at"] = now if pending == "error" else 0
                try:
                    state.save()
                except (OSError, TypeError, ValueError):
                    pass


def poll_once() -> None:
    """Refresh once and archive yesterday's actual API window, never synthesized data."""
    if state.maintenance_active() or not config.OWNER_ADDRESS or state.get_kill_switch(config.OWNER_ADDRESS):
        return
    try:
        with state.guard_work(config.OWNER_ADDRESS):
            rates = tools.current_rates()
            if not rate_store.save_latest(rates, tools.rates_observed_at()):
                raise tools.RateError("storage")
            yesterday = (datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)).isoformat()
            if not rate_store.day(yesterday) and tools._eltoque_history_lock.acquire(blocking=False):
                try:
                    values = tools._eltoque_request({"date_from": yesterday + " 00:00:00", "date_to": yesterday + " 23:59:59"})
                    if not rate_store.save_days({yesterday: {"rates": values, "at": time.time()}}):
                        raise tools.RateError("storage")
                finally:
                    tools._eltoque_history_lock.release()
    except state.WorkPaused:
        return
    except tools.RateError as exc:
        record_health(str(exc))
    except Exception:
        record_health("http")
    else:
        record_health()


def run(stop: threading.Event) -> None:
    """Run in a daemon thread, pause cooperatively and resume after maintenance."""
    due = 0.0
    while not stop.is_set():
        paused = state.maintenance_active() or (config.OWNER_ADDRESS and state.get_kill_switch(config.OWNER_ADDRESS))
        if paused:
            due = 0.0
            stop.wait(5)
            continue
        if time.monotonic() >= due:
            poll_once()
            due = time.monotonic() + config.ELTOQUE_REFRESH_SECONDS
        stop.wait(min(30, max(1.0, due - time.monotonic())))
