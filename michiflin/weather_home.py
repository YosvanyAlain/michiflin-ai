"""Per-person weather snapshots and a quiet, bounded server-side refresh.

No device location is read. Places are explicitly typed/selected; the owner's
initial city is the app's existing Havana default. Other people must enable
background refresh themselves. Snapshots and city preferences live in the bot
state (and its encrypted backups); no proactive chat messages are sent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from typing import Any

from . import access, admin, config, state, tools

INTERVAL = 6 * 3600
DEFAULT_CITY = "La Habana, Cuba"
MAX_RECORDS = 500  # refuse new records instead of purging older people's data
MAX_SNAPSHOT_BYTES = 16_000
_fetch_lock = threading.Lock()


class WeatherHomeError(Exception):
    pass


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _scope(address: str) -> tuple[str, str, str] | None:
    address = str(address or "").strip().lower()[:320]
    if not address or not admin.is_allowed(address):
        return None
    owner = config.is_owner_admin(address)
    stamp = "owner" if owner else access.session_stamp(address)
    if not owner and not stamp:
        if config.INVITE_AUTO_ACCESS or access.snapshot().get("controlled"):
            return None
        stamp = "legacy"
    epoch = config.BOT_EPOCH if config.BOT_EPOCH_STRICT else ""
    key = hashlib.sha256((address + "\0" + stamp + "\0" + epoch).encode("utf-8")).hexdigest()
    return key, address, stamp


def _records() -> dict[str, Any]:
    return state.data.setdefault("weather_home", {})


def _ensure(scope: tuple[str, str, str]) -> dict[str, Any]:
    key, address, stamp = scope
    with state.lock:
        records = _records()
        record = records.get(key)
        if isinstance(record, dict):
            return record
        if len(records) >= MAX_RECORDS:
            raise WeatherHomeError("full")
        record = {"address": address, "stamp": stamp, "city": DEFAULT_CITY, "place_id": 0,
                  "auto": bool(config.OWNER_ADDRESS and address == config.OWNER_ADDRESS.casefold()),
                  "next_at": 0.0, "revision": 0, "last": None}
        records[key] = record
        try:
            state.save()
        except (OSError, ValueError, TypeError) as exc:
            records.pop(key, None)
            raise WeatherHomeError("storage") from exc
        return record


def _localized(snapshot: Any, locale: str) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("place"), dict) or not isinstance(snapshot.get("current"), dict):
        return None
    result = copy.deepcopy(snapshot)
    index = 1 if locale == "en" else 0
    for item in [result["current"], *(result.get("days") if isinstance(result.get("days"), list) else [])]:
        if not isinstance(item, dict):
            continue
        try:
            item["description"] = tools.WEATHER.get(int(item.get("code")), ("?", "?"))[index]
        except (TypeError, ValueError):
            pass
    return result


def view(address: str, locale: str) -> dict[str, Any] | None:
    """Return only this currently authorized person's saved data; no network."""
    scope = _scope(address)
    if not scope:
        return None
    _ensure(scope)
    with state.lock:
        record = _records().get(scope[0])
        if not isinstance(record, dict):
            raise WeatherHomeError("storage")
        last = record.get("last") if isinstance(record.get("last"), dict) else {}
        snapshot = _localized(last.get("data"), locale)
        return {"city": str(record.get("city") or DEFAULT_CITY)[:120],
                "place_id": record.get("place_id") if type(record.get("place_id")) is int else 0,
                "auto": record.get("auto") is True, "next_at": _number(record.get("next_at")),
                "interval": INTERVAL,
                "last": {"at": _number(last.get("at")), "data": snapshot} if snapshot else None}


def _store_change(scope: tuple[str, str, str], mutate: Any) -> None:
    _ensure(scope)
    key = scope[0]
    with state.lock:
        records = _records()
        record = records.get(key)
        if not isinstance(record, dict):
            raise WeatherHomeError("storage")
        previous = copy.deepcopy(record)
        try:
            state.check_work_allowed()
            mutate(record)
            state.save()
        except WeatherHomeError:
            records[key] = previous
            raise
        except (OSError, ValueError, TypeError) as exc:
            records[key] = previous
            raise WeatherHomeError("storage") from exc


def _snapshot(data: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("large snapshot")
        clean = json.loads(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise WeatherHomeError("snapshot") from exc
    if not isinstance(clean, dict) or not isinstance(clean.get("current"), dict) or not isinstance(clean.get("place"), dict):
        raise WeatherHomeError("snapshot")
    return clean


def _same_default(record: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    place = snapshot.get("place") if isinstance(snapshot.get("place"), dict) else {}
    selected = record.get("place_id")
    if selected:
        return selected == place.get("id")
    default = [part.strip().casefold() for part in str(record.get("city") or "").split(",") if part.strip()]
    query = [part.strip().casefold() for part in str(snapshot.get("query") or "").split(",") if part.strip()]
    label = str(place.get("label") or "").casefold()
    # A manually queried "La Habana" is also the configured "La Habana,
    # Cuba" only when the actual selected result confirms the country.
    return bool(default and query and default[0] == query[0] and all(part in label for part in default[1:]))


def remember(address: str, data: dict[str, Any], at: float | None = None) -> tuple[float, bool]:
    """Save a manual lookup without changing the user's chosen default."""
    scope = _scope(address)
    timestamp = time.time() if at is None else _number(at)
    if not scope:
        return timestamp, False
    try:
        snapshot = _snapshot(data)
        def apply(record: dict[str, Any]) -> None:
            record["last"] = {"at": timestamp, "data": snapshot}
            if record.get("auto") is True and _same_default(record, snapshot):
                record["next_at"] = timestamp + INTERVAL
                record["revision"] = int(record.get("revision") or 0) + 1
        _store_change(scope, apply)
        return timestamp, True
    except (WeatherHomeError, ValueError, TypeError):
        return timestamp, False  # caller can keep a device-local copy


def query(address: str, city: str, locale: str, place_id: int = 0) -> dict[str, Any]:
    """Serialize weather fetches; manual requests are always deliberate."""
    with _fetch_lock:
        state.check_work_allowed()
        data = tools.weather_data(city, locale, place_id)
        state.check_work_allowed()
        at, saved = remember(address, data)
    return {**data, "saved_at": at, "saved_on_bot": saved}


def settings(address: str, action: str, value: Any = None) -> None:
    scope = _scope(address)
    if not scope:
        raise WeatherHomeError("access")
    timestamp = time.time()
    if action == "auto" and type(value) is bool:
        def apply(record: dict[str, Any]) -> None:
            if record.get("auto") is value:
                return
            record["auto"] = value
            record["revision"] = int(record.get("revision") or 0) + 1
            if value:
                last = record.get("last") if isinstance(record.get("last"), dict) else {}
                recent = last.get("data") if isinstance(last.get("data"), dict) else {}
                from_last = _number(last.get("at")) + INTERVAL if _same_default(record, recent) else 0.0
                # Toggling refresh off and on must not erase a failed attempt's
                # persisted backoff or cause an immediate duplicate request.
                record["next_at"] = max(_number(record.get("next_at")), from_last)
        _store_change(scope, apply)
    elif action == "use_last" and isinstance(value, dict):
        def apply(record: dict[str, Any]) -> None:
            last = record.get("last") if isinstance(record.get("last"), dict) else {}
            snapshot = last.get("data") if isinstance(last.get("data"), dict) else {}
            place = snapshot.get("place") if isinstance(snapshot.get("place"), dict) else {}
            city, place_id = str(snapshot.get("query") or "").strip()[:120], place.get("id")
            if not city or type(place_id) is not int or place_id <= 0:
                raise WeatherHomeError("no_result")
            # Never select a different city if another device changed the last result.
            if value.get("city") != city or value.get("place_id") != place_id:
                raise WeatherHomeError("changed")
            record["city"], record["place_id"] = city, place_id
            record["next_at"] = max(timestamp, _number(last.get("at")) + INTERVAL)
            record["revision"] = int(record.get("revision") or 0) + 1
        _store_change(scope, apply)
    else:
        raise WeatherHomeError("invalid")


def poll_once(at: float | None = None) -> bool:
    """Attempt at most one authorized due place, without sending messages.

    Reserve the next attempt durably *before* network access. Restarts, errors,
    rate limits and maintenance therefore cannot create a retry storm. A newer
    manual query or changed preference wins over an in-flight background result.
    """
    timestamp = time.time() if at is None else _number(at)
    if state.maintenance_active() or not _fetch_lock.acquire(blocking=False):
        return False
    try:
        with state.lock:
            due = sorted(((_number(row.get("next_at")), key, str(row.get("address") or ""))
                          for key, row in _records().items() if isinstance(row, dict) and row.get("auto") is True
                          and _number(row.get("next_at")) <= timestamp), key=lambda item: (item[0], item[1]))
        for _when, key, address in due:
            scope = _scope(address)
            if not scope or scope[0] != key or state.get_kill_switch(address):
                continue
            try:
                with state.guard_work(address):
                    with state.lock:
                        record = _records().get(key)
                        if (not isinstance(record, dict) or record.get("auto") is not True
                                or _number(record.get("next_at")) > timestamp):
                            continue
                        city, place_id = str(record.get("city") or "")[:120], record.get("place_id")
                        if not city or type(place_id) is not int or place_id < 0:
                            continue
                        revision = int(record.get("revision") or 0)
                        previous_due = record.get("next_at")
                        record["next_at"] = timestamp + INTERVAL
                        try:
                            state.save()
                        except (OSError, TypeError, ValueError):
                            record["next_at"] = previous_due
                            return False  # never fetch if we cannot persist the throttle
                    data = tools.weather_data(city, "es", place_id)
                    state.check_work_allowed()
                    snapshot = _snapshot(data)
                    current_scope = _scope(address)
                    if not current_scope or current_scope[0] != key:
                        return True
                    with state.lock:
                        state.check_work_allowed()
                        record = _records().get(key)
                        last = record.get("last") if isinstance(record, dict) and isinstance(record.get("last"), dict) else {}
                        if (isinstance(record, dict) and record.get("auto") is True
                                and int(record.get("revision") or 0) == revision
                                and _number(last.get("at")) <= timestamp):
                            previous = copy.deepcopy(record.get("last"))
                            record["last"] = {"at": time.time() if at is None else timestamp, "data": snapshot}
                            try:
                                state.save()
                            except (OSError, TypeError, ValueError):
                                record["last"] = previous
                return True
            except (state.WorkPaused, tools.WeatherError, WeatherHomeError, OSError, ValueError, TypeError):
                return True  # reservation remains; no automatic immediate retry
        return False
    finally:
        _fetch_lock.release()


def run(stop: threading.Event) -> None:
    """Cooperatively pause with maintenance, the kill switch and shutdown."""
    while not stop.is_set():
        try:
            attempted = poll_once()
        except Exception:
            # Do not expose addresses, city names or provider errors in logs.
            attempted = False
        stop.wait(30 if attempted else 60)
