"""Persistent runtime state: settings, job metadata, notes, links and usage.

State may include contact addresses and short samples of denied messages. The
separate Delta Chat account and backups can contain additional sensitive data.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from contextlib import contextmanager

from . import access, config
from .config import CONFIG_DIR, DEFAULTS, LEGACY_STATE_FILE, STATE_FILE
from .util import utc_day

VERSION = 5
LINK_LIMIT = 2000
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

lock = threading.RLock()
data: dict[str, Any] = {
    "version": VERSION,
    "settings": {},
    "jobs": {},
    "usage": {},
    "cooldowns": {},
    "reasons": {},
    "remaining": {},
    "links": {},
    "link_seq": 0,
    "sent": [],
    "flags": {},
    "access": {},
    "apps": {},
    "people": {},
    "prefs": {},
    "notes": {},
    "link_queue": {},
    "link_seq_q": 0,
    "nauta_strict": {},
    "kill_switch": {},
    "maintenance_notice": {},
    "personal_ai_budgets": {},
}

# Settings each person keeps for themselves; everything else is shared and changed only by administrators.
PERSONAL = frozenset({
    "language", "video_height", "audio_kbps", "audio_format", "part_mb", "part_format", "auto_send", "batch", "delay",
    "window", "daily_cap_mb", "reader_kb", "reader_images", "reader_links", "capture_device", "capture_quality",
    "notify", "progress", "reactions", "chat_mode", "image_size", "ai_pin",
})
_acting = threading.local()
# A transition to ON invalidates work already admitted, even if OFF follows immediately.
_kill_generations: dict[str, int] = {}

# Allowed values for each owner setting: a set of choices or an inclusive int range.
SETTING_RULES: dict[str, Any] = {
    "language": {"auto", "es", "en"},
    "video_height": {144, 240, 360, 480, 720, 1080, 1440, 2160},
    "audio_kbps": {32, 48, 64, 96, 128, 160, 192, 224, 256, 320},
    "audio_format": {"m4a", "mp3", "opus"},
    "part_mb": (1, 20),
    "part_format": {"7z", "zip", "raw"},
    "batch": (0, 200),
    "delay": (0, 3600),
    "auto_send": {True, False},
    "daily_cap_mb": (0, 100000),
    "reader_images": {"off", "low"},
    "reader_kb": (30, 900),
    "reader_links": {"chat", "web"},
    "capture_device": {"mobile", "desktop"},
    "notify": {"minimal", "normal"},
    "progress": {"on", "off"},
    "reactions": {"on", "off", "all"},
    "chat_mode": {"app", "full"},
    "capture_quality": {"normal", "high", "max"},
    "image_size": {512, 768, 1024},
    "ttl_hours": (1, 336),
    "user_jobs": (1, 20),
    "user_daily_mb": (0, 100000),
    "ai_daily": (0, 1000),
    "image_daily": (0, 200),
    "code_daily": (0, 50),
    "ai_reserve": (0, 90),
}


def _valid(key: str, value: Any) -> bool:
    rule = SETTING_RULES.get(key)
    if rule is None:
        return key in {"window", "ai_pin"} and isinstance(value, str)
    if isinstance(rule, tuple):
        return isinstance(value, int) and not isinstance(value, bool) and rule[0] <= value <= rule[1]
    return value in rule


def act_as(address: str) -> None:
    """Personal settings read and written by this thread belong to this person; administrators use the shared ones."""
    address = (address or "").strip().lower()
    _acting.address = "" if address in config.ADMIN_ADDRESSES else address
    _acting.sender = address
    _acting.charged = False
    _acting.personal_ai_secure = False


def acting_as() -> str:
    return str(getattr(_acting, "address", ""))


def request_sender() -> str:
    """Address behind the current request, administrators included."""
    return str(getattr(_acting, "sender", ""))


def set_personal_ai_secure(value: bool) -> None:
    """Set only after validating this request's current encrypted single chat."""
    _acting.personal_ai_secure = bool(value)


def personal_ai_secure() -> bool:
    return bool(getattr(_acting, "personal_ai_secure", False))


def charge_once() -> bool:
    """True the first time per request: one request spends one unit of a daily allowance."""
    if getattr(_acting, "charged", False):
        return False
    _acting.charged = True
    return True


def uncharge() -> None:
    _acting.charged = False


@contextmanager
def person(address: str):
    previous = (acting_as(), request_sender(), bool(getattr(_acting, "charged", False)), personal_ai_secure())
    act_as(address)
    try:
        yield
    finally:
        _acting.address, _acting.sender, _acting.charged, _acting.personal_ai_secure = previous


def _shared(key: str) -> Any:
    with lock:
        value = data["settings"].get(key, DEFAULTS.get(key))
    if not _valid(key, value):
        value = DEFAULTS.get(key)
        if not _valid(key, value):
            rule = SETTING_RULES.get(key)
            value = rule[0] if isinstance(rule, tuple) else sorted(rule, key=str)[0] if rule else ""
    return value


def setting(key: str) -> Any:
    address = acting_as()
    if address and key in PERSONAL:
        with lock:
            own = data["prefs"].get(address) or {}
            if key in own and _valid(key, own[key]):
                return own[key]
    return _shared(key)


def can_change(key: str) -> bool:
    return not acting_as() or key in PERSONAL


def own_settings() -> None:
    """Give the acting person their own copy, starting from the current shared values."""
    address = acting_as()
    if not address:
        return
    with lock:
        if address in data["prefs"]:
            return
        data["prefs"][address] = {key: _shared(key) for key in PERSONAL}
    _save_soon()


def reset_own_settings() -> None:
    address = acting_as()
    if address:
        with lock:
            data["prefs"].pop(address, None)
        own_settings()


def forget_person(address: str) -> None:
    with lock:
        data["prefs"].pop((address or "").lower(), None)
    _save_soon()


def set_setting(key: str, value: Any) -> bool:
    if not _valid(key, value) or not can_change(key):
        return False
    address = acting_as()
    with lock:
        if address:
            data["prefs"].setdefault(address, {key: _shared(key) for key in PERSONAL})[key] = value
        else:
            data["settings"][key] = value
    save()
    return True


def save() -> None:
    with lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if access.preserve_history():
            # Retain historical non-owner/unknown delivery records; do not purge
            # old people as a side effect of saving the current owner's state.
            history = [item for item in data["sent"] if len(item) < 3 or str(item[2]).lower() != config.OWNER_ADDRESS]
            current = [item for item in data["sent"] if len(item) >= 3 and str(item[2]).lower() == config.OWNER_ADDRESS
                       and now - item[0] < 86400][-2000:]
            data["sent"] = history + current
        else:
            data["sent"] = [item for item in data["sent"] if now - item[0] < 86400][-2000:]
        _prune_usage()
        temporary = STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(STATE_FILE)


def load() -> bool:
    """Load state. Preserve legacy files; their unsupported queue is not activated."""
    migrated = False
    with lock:
        if STATE_FILE.is_file():
            try:
                stored = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("Cannot read runtime state; refusing to overwrite it") from exc
            if not isinstance(stored, dict) or stored.get("version") != VERSION:
                raise RuntimeError("Unsupported runtime state version; refusing to overwrite it")
            for key in data:
                if key in stored and not isinstance(stored[key], type(data[key])):
                    raise RuntimeError(f"Invalid runtime state field {key}; refusing to overwrite it")
            # Keep unknown fields too: they may belong to a newer or older runtime.
            data.update(stored)
        if LEGACY_STATE_FILE.exists() and not data["flags"].get("legacy_state_seen"):
            # Import only the compatible language setting; preserve the legacy file for the owner.
            try:
                legacy = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
                old = legacy.get("settings", {}) if isinstance(legacy, dict) else {}
                if old.get("language") in {"auto", "es", "en"}:
                    data["settings"].setdefault("language", old["language"])
            except (OSError, ValueError, AttributeError):
                pass
            data["flags"]["legacy_state_seen"] = True
            migrated = True
    save()
    return migrated


# ---------------------------------------------------------------------------
# Notes (B1): short encrypted-in-state notes, owner only.
# ---------------------------------------------------------------------------
def _person_notes(address: str) -> list[dict[str, Any]]:
    with lock:
        return list(data["notes"].get(address.lower(), []))


def list_notes(address: str) -> list[dict[str, Any]]:
    return _person_notes(address)


def add_note(address: str, text: str) -> int:
    address = (address or "").strip().lower()
    with lock:
        notes = data["notes"].setdefault(address, [])
        note = {"id": len(notes) + 1, "text": text, "ts": time.time()}
        notes.append(note)
    _save_soon()
    return note["id"]


def delete_note(address: str, note_id: int) -> bool:
    address = (address or "").strip().lower()
    with lock:
        notes = data["notes"].get(address, [])
        for index, note in enumerate(notes):
            if note["id"] == note_id:
                notes.pop(index)
                _save_soon()
                return True
    return False


def clear_notes(address: str) -> int:
    address = (address or "").strip().lower()
    with lock:
        count = len(data["notes"].get(address, []))
        data["notes"][address] = []
    _save_soon()
    return count


# ---------------------------------------------------------------------------
# Read-later link queue (B2): URLs pending to read/download, owner only.
# ---------------------------------------------------------------------------
def _person_links(address: str) -> list[dict[str, Any]]:
    with lock:
        return list(data["link_queue"].get(address.lower(), []))


def list_links(address: str) -> list[dict[str, Any]]:
    return _person_links(address)


def add_link(address: str, url: str, title: str = "") -> int:
    address = (address or "").strip().lower()
    with lock:
        queue = data["link_queue"].setdefault(address, [])
        data["link_seq_q"] += 1
        item = {"id": data["link_seq_q"], "url": url, "title": title, "ts": time.time()}
        queue.append(item)
    _save_soon()
    return item["id"]


def remove_link(address: str, link_id: int) -> bool:
    address = (address or "").strip().lower()
    with lock:
        queue = data["link_queue"].get(address, [])
        for index, item in enumerate(queue):
            if item["id"] == link_id:
                queue.pop(index)
                _save_soon()
                return True
    return False


def next_link(address: str) -> dict[str, Any] | None:
    """Return and remove the oldest pending link for this person."""
    address = (address or "").strip().lower()
    with lock:
        queue = data["link_queue"].get(address, [])
        if not queue:
            return None
        item = queue.pop(0)
        _save_soon()
        return item


def clear_links(address: str) -> int:
    address = (address or "").strip().lower()
    with lock:
        count = len(data["link_queue"].get(address, []))
        data["link_queue"][address] = []
    _save_soon()
    return count


# ---------------------------------------------------------------------------
# Nauta strict mode (A1): per-person data-saving profile.
# ---------------------------------------------------------------------------
def get_nauta_strict(address: str) -> bool:
    address = (address or "").strip().lower()
    with lock:
        return bool(data["nauta_strict"].get(address, DEFAULTS.get("nauta_strict", False)))


def set_nauta_strict(address: str, value: bool) -> None:
    address = (address or "").strip().lower()
    with lock:
        data["nauta_strict"][address] = bool(value)
    save()


# ---------------------------------------------------------------------------
# Kill switch (F2): per-person pause of queue + AI + downloads.
# ---------------------------------------------------------------------------
class WorkPaused(Exception):
    """A request was invalidated by a pause or an access change."""

    def __init__(self, maintenance: bool = False):
        super().__init__("maintenance" if maintenance else "paused")
        self.maintenance = maintenance


def get_kill_switch(address: str) -> bool:
    address = (address or "").strip().lower()
    with lock:
        return bool(data["kill_switch"].get(address, DEFAULTS.get("kill_switch", False)))


def maintenance_active() -> bool:
    """The owner's operational pause; it never changes the access grants."""
    from . import maintenance
    maintenance.expire_if_due()
    with lock:
        notice = data.get("maintenance_notice")
        return isinstance(notice, dict) and notice.get("active") is True


def maintenance_generation() -> int:
    with lock:
        notice = data.get("maintenance_notice")
        return int(notice.get("generation") or 0) if isinstance(notice, dict) else 0


def kill_generation(address: str) -> int:
    address = (address or "").strip().lower()
    with lock:
        # Keep access.revision's full 128 bits. The persistent maintenance epoch
        # also invalidates work if maintenance is switched on and off quickly.
        return ((_kill_generations.get(address, 0) << 256)
                + (maintenance_generation() << 128) + access.revision(address))


def maintenance_interrupted(generation: int) -> bool:
    return maintenance_active() or ((generation >> 128) & ((1 << 128) - 1)) != maintenance_generation()


def work_interrupted(address: str, generation: int) -> bool:
    address = (address or "").strip().lower()
    with lock:
        return (maintenance_active() or get_kill_switch(address) or kill_generation(address) != generation
                or bool(address and not config.service_access_allowed(address)))


def check_work_allowed() -> None:
    guard = getattr(_acting, "kill_guard", None)
    if guard and work_interrupted(*guard):
        raise WorkPaused(maintenance=maintenance_interrupted(guard[1]))


@contextmanager
def guard_work(address: str, generation: int | None = None):
    """Cooperative guard for one request; nested operations share the admission generation."""
    address = (address or "").strip().lower()
    previous = getattr(_acting, "kill_guard", None)
    _acting.kill_guard = (address, kill_generation(address) if generation is None else generation)
    try:
        check_work_allowed()
        yield
        check_work_allowed()
    finally:
        _acting.kill_guard = previous


def set_kill_switch(address: str, value: bool) -> None:
    address = (address or "").strip().lower()
    with lock:
        active = get_kill_switch(address)
        data["kill_switch"][address] = bool(value)
        if value and not active:
            _kill_generations[address] = _kill_generations.get(address, 0) + 1
    save()


# ---------------------------------------------------------------------------
# Link registry: short ids make URLs tappable as chat commands (/v_ab).
# ---------------------------------------------------------------------------
def _encode(number: int) -> str:
    text = ""
    while True:
        number, rest = divmod(number, 36)
        text = _ALPHABET[rest] + text
        if not number:
            return text


def link_id(url: str) -> str:
    with lock:
        links: dict[str, Any] = data["links"]
        for key, entry in links.items():
            if entry[0] == url:
                entry[1] = time.time()
                return key
        data["link_seq"] += 1
        key = "k" + _encode(data["link_seq"])
        links[key] = [url, time.time()]
        if len(links) > LINK_LIMIT:
            for old in sorted(links, key=lambda item: links[item][1])[: len(links) - LINK_LIMIT]:
                links.pop(old, None)
    _save_soon()
    return key


_save_timer: threading.Timer | None = None


def _save_soon() -> None:
    """Coalesce frequent small changes into one write."""
    global _save_timer
    with lock:
        if _save_timer is None or not _save_timer.is_alive():
            _save_timer = threading.Timer(3.0, save)
            _save_timer.daemon = True
            _save_timer.start()


def resolve_link(token: str) -> str:
    token = (token or "").strip().lower()
    with lock:
        entry = data["links"].get(token)
    return entry[0] if entry else ""


# ---------------------------------------------------------------------------
# AI usage accounting (per provider/model per UTC day) and cooldowns.
# ---------------------------------------------------------------------------
def _prune_usage() -> None:
    today = utc_day()
    if not access.preserve_history():
        data["usage"] = {day: value for day, value in data["usage"].items() if day >= _days_ago(7)}
    data["usage"].setdefault(today, {})
    now = time.time()
    data["cooldowns"] = {key: until for key, until in data["cooldowns"].items() if until > now}
    data["reasons"] = {key: reason for key, reason in data["reasons"].items() if key in data["cooldowns"]}


def _days_ago(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def record_usage(key: str, ok: bool, tokens: int = 0) -> None:
    with lock:
        day = data["usage"].setdefault(utc_day(), {})
        entry = day.setdefault(key, [0, 0, 0])
        entry[0] += 1
        entry[1] += 0 if ok else 1
        entry[2] += max(0, int(tokens))
    _save_soon()


def usage_today(key: str) -> int:
    """Successful uses today; failed attempts do not spend the visible quota."""
    with lock:
        entry = data["usage"].get(utc_day(), {}).get(key, [0, 0])
    return max(0, int(entry[0]) - int(entry[1] if len(entry) > 1 else 0))


def usage_report_rows() -> list[tuple[str, int, int, int]]:
    with lock:
        day = data["usage"].get(utc_day(), {})
        return sorted((key, value[0], value[1], value[2]) for key, value in day.items())


def set_cooldown(key: str, seconds: int, reason: str = "") -> None:
    with lock:
        data["cooldowns"][key] = time.time() + max(1, seconds)
        if reason:
            data["reasons"][key] = reason
        else:
            data["reasons"].pop(key, None)
    _save_soon()


def until_utc_midnight() -> int:
    """Seconds until the next daily reset of quotas counted in UTC days."""
    now = time.time()
    return int(86400 - now % 86400) + 60


def cooldown_reason(key: str) -> str:
    with lock:
        return str(data["reasons"].get(key, "")) if cooldown_until(key) else ""


def cooldown_until(key: str) -> float:
    with lock:
        until = float(data["cooldowns"].get(key, 0))
    return until if until > time.time() else 0.0


def set_remaining(key: str, remaining: int, limit: int) -> None:
    with lock:
        data["remaining"][key] = [int(remaining), int(limit), time.time()]


def remaining_for(key: str) -> tuple[int, int] | None:
    with lock:
        entry = data["remaining"].get(key)
    if not entry or time.time() - entry[2] > 6 * 3600:
        return None
    return int(entry[0]), int(entry[1])


def record_sent(size: int, address: str = "") -> None:
    with lock:
        data["sent"].append([time.time(), int(size), address.lower()] if address else [time.time(), int(size)])


def sent_last_day() -> int:
    now = time.time()
    with lock:
        return sum(int(item[1]) for item in data["sent"] if now - item[0] < 86400)


def flag(name: str) -> Any:
    with lock:
        return data["flags"].get(name)


def set_flag(name: str, value: Any) -> None:
    with lock:
        data["flags"][name] = value
    save()
