"""Persist only received elTOQUE observations, never its credential.

The current quote and requested day windows are kept in the existing encrypted
state backup. No provider values are invented or silently treated as live.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

from . import state


def _rates(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9_-]{2,32}", key) or isinstance(item, bool):
            continue
        try:
            number = float(item)
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(number) and number > 0:
            result[key] = number
    return result


def _entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    rates = _rates(value.get("rates"))
    try:
        at = float(value.get("at") or 0)
    except (ValueError, TypeError, OverflowError):
        return None
    if not rates or not math.isfinite(at) or at <= 0 or at > time.time() + 300:
        return None
    return {"rates": rates, "at": at}


def latest() -> dict[str, Any] | None:
    with state.lock:
        section = state.data.get("eltoque")
        return _entry(section.get("latest")) if isinstance(section, dict) else None


def day(date: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
        return None
    with state.lock:
        section = state.data.get("eltoque")
        days = section.get("days", {}) if isinstance(section, dict) else {}
        return _entry(days.get(date)) if isinstance(days, dict) else None


def status() -> dict[str, Any]:
    with state.lock:
        section = state.data.get("eltoque")
        health = section.get("health", {}) if isinstance(section, dict) else {}
        if not isinstance(health, dict):
            return {}
        return {"error": str(health.get("error") or "")[:32], "since": health.get("since", 0),
                "ok_at": health.get("ok_at", 0)}


def save_latest(rates: dict[str, float], observed: float) -> bool:
    item = _entry({"rates": rates, "at": observed})
    if item is None:
        return False
    with state.lock:
        section = state.data.setdefault("eltoque", {})
        old = _entry(section.get("latest"))
        if old and abs(old["at"] - item["at"]) < 1 and old["rates"] == item["rates"]:
            return True
        section["latest"] = item
        try:
            state.save()
            return True
        except (OSError, TypeError, ValueError):
            if old is None:
                section.pop("latest", None)
            else:
                section["latest"] = old
            return False


def save_days(items: dict[str, dict[str, Any]]) -> bool:
    """Atomically persist received windows; preserve all existing valid dates."""
    clean = {date: item for date, raw in items.items()
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)) and (item := _entry(raw)) is not None}
    if not clean:
        return True
    with state.lock:
        section = state.data.setdefault("eltoque", {})
        days = section.setdefault("days", {})
        if not isinstance(days, dict):
            return False
        old = dict(days)
        days.update(clean)
        try:
            state.save()
            return True
        except (OSError, TypeError, ValueError):
            days.clear()
            days.update(old)
            return False
