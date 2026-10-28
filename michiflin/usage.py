"""Per-person usage for fair sharing: daily counters only, never what was asked or downloaded."""
from __future__ import annotations

import time
from typing import Any

from . import access, admin, config, state
from .util import local_time, tr, utc_day

DAYS = 7
HEAVY_SHARE = 0.5             # more than half of all traffic among two or more active people
HEAVY_MIN_BYTES = 200 * 1024 * 1024
HEAVY_MIN_REQUESTS = 150
FIELDS = ("bytes", "jobs", "requests", "ai", "images", "code")
# Daily AI allowances for people who are not administrators: kind -> (setting, counter).
ALLOWANCES = {"ask": ("ai_daily", "ai"), "image": ("image_daily", "images"), "code": ("code_daily", "code")}


def _book() -> dict[str, dict[str, list[int]]]:
    with state.lock:
        book = state.data.get("people")
        if not isinstance(book, dict):
            book = state.data["people"] = {}
        return book


def note(address: str, **counts: int) -> None:
    """Add to today's counters of one person (bytes delivered, jobs queued, app requests, AI requests)."""
    address = (address or "").strip().lower()
    if not address or not any(counts.values()):
        return
    day = utc_day()
    with state.lock:
        days = _book().setdefault(address, {})
        row = days.setdefault(day, [0] * len(FIELDS))
        row.extend([0] * (len(FIELDS) - len(row)))
        for index, name in enumerate(FIELDS):
            row[index] = int(row[index]) + int(counts.get(name, 0))
        if not access.preserve_history():
            for stale in sorted(days)[:-DAYS]:
                days.pop(stale, None)
    state._save_soon()


def totals(days: int = DAYS) -> dict[str, list[int]]:
    keep = {time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400 * back)) for back in range(days)}
    found: dict[str, list[int]] = {}
    with state.lock:
        for address, rows in _book().items():
            if not config.service_access_allowed(address):
                continue  # preserved historical usage is not an active person's share
            total = [0] * len(FIELDS)
            for day, row in rows.items():
                if day in keep:
                    for index, value in enumerate(row[:len(FIELDS)]):
                        total[index] += int(value)
            if any(total):
                found[address] = total
    return found


def sent_today(address: str) -> int:
    """Bytes delivered to this person in the last 24 hours."""
    address = (address or "").lower()
    now = time.time()
    with state.lock:
        return sum(int(item[1]) for item in state.data["sent"] if len(item) > 2 and item[2] == address and now - item[0] < 86400)


def over_budget(address: str) -> bool:
    """A non-admin over the daily budget is served last, never refused."""
    budget = int(state.setting("user_daily_mb")) * 1024 * 1024
    return bool(budget) and not admin.is_admin(address or "") and sent_today(address) >= budget


def job_owner(job: dict[str, Any]) -> str:
    return str(job.get("addr") or f"chat:{job.get('chat')}")


def priority(job: dict[str, Any], busy: dict[str, int] | None = None) -> tuple:
    """Order for the next turn: in-budget first, people with fewer running jobs, less delivered today, then oldest."""
    owner = job_owner(job)
    return (over_budget(owner), (busy or {}).get(owner, 0), sent_today(owner), int(job["id"]))


def report() -> dict[str, Any]:
    """Shares of the last days and who uses a disproportionate part of the bot (amounts only)."""
    week = totals(DAYS)
    day = totals(1)
    all_bytes = sum(row[0] for row in week.values())
    all_requests = sum(row[2] for row in week.values())
    active = sum(1 for row in week.values() if row[0] or row[2])
    people = []
    for address, row in sorted(week.items(), key=lambda item: (-item[1][0], -item[1][2])):
        share_bytes = row[0] / all_bytes if all_bytes else 0.0
        share_requests = row[2] / all_requests if all_requests else 0.0
        heavy = active >= 2 and ((share_bytes > HEAVY_SHARE and row[0] >= HEAVY_MIN_BYTES)
                                 or (share_requests > HEAVY_SHARE and row[2] >= HEAVY_MIN_REQUESTS))
        today = day.get(address, [0] * len(FIELDS))
        people.append({"address": address, "bytes": row[0], "jobs": row[1], "requests": row[2], "ai": row[3], "images": row[4], "code": row[5],
                       "share": round(share_bytes, 3), "share_requests": round(share_requests, 3), "heavy": heavy,
                       "today": today[0], "over": over_budget(address), "admin": admin.is_admin(address)})
    return {"days": DAYS, "bytes": all_bytes, "requests": all_requests, "active": active, "people": people,
            "budget_mb": int(state.setting("user_daily_mb")), "max_jobs": int(state.setting("user_jobs")),
            "paused": bool(state.flag("queue_paused")), "limited": sorted(state.flag("limited") or []), "ai": _ai_rules()}


def _ai_rules() -> dict[str, int]:
    from . import ai

    return {"ask": int(state.setting("ai_daily")), "image": int(state.setting("image_daily")), "code": int(state.setting("code_daily")),
            "reserve": int(state.setting("ai_reserve")), "left": round(ai.capacity_left() * 100)}


def used_today(address: str, kind: str) -> int:
    index = FIELDS.index(ALLOWANCES[kind][1])
    with state.lock:
        row = _book().get((address or "").lower(), {}).get(utc_day()) or []
    return int(row[index]) if index < len(row) else 0


def limit_for(address: str, kind: str) -> int:
    """Daily allowance of one kind; 0 means unlimited (always for administrators)."""
    if not address or admin.is_admin(address):
        return 0
    return int(state.setting(ALLOWANCES[kind][0]))


def next_reset() -> float:
    return (int(time.time()) // 86400 + 1) * 86400.0


def allowed(address: str, kind: str) -> bool:
    limit = limit_for(address, kind)
    return not limit or used_today(address, kind) < limit


def spend(address: str, kind: str) -> None:
    note(address, **{ALLOWANCES[kind][1]: 1})


def reserve_low() -> bool:
    """True when the shared AI capacity left today is below the administrators' reserve."""
    from . import ai

    reserve = int(state.setting("ai_reserve"))
    return bool(reserve) and ai.capacity_left() * 100 < reserve


def limit_text(kind: str, locale: str) -> str:
    address = state.request_sender()
    at = local_time(next_reset(), "%H:%M")
    limit = limit_for(address, kind)
    words = {"ask": ("consultas de IA", "AI requests"), "image": ("imágenes", "images"), "code": ("proyectos del Programador", "Coder projects")}[kind]
    return tr(locale, f"Llegaste a tus {limit} {words[0]} de hoy. Se renuevan a las {at}.",
              f"You reached your {limit} {words[1]} for today. They renew at {at}.")


def reserve_text(locale: str) -> str:
    at = local_time(next_reset(), "%H:%M")
    return tr(locale, f"Queda poca IA por hoy y se reserva para la administración; usa un modelo rápido o vuelve después de las {at}.",
              f"Little AI is left today and it is reserved for administration; use a fast model or come back after {at}.")


def quota_view(address: str) -> dict[str, Any] | None:
    """What a person has left today, for the app; None for administrators."""
    if not address or admin.is_admin(address):
        return None
    view: dict[str, Any] = {"reset": next_reset(), "low": reserve_low()}
    for kind in ALLOWANCES:
        view[kind] = {"used": used_today(address, kind), "limit": limit_for(address, kind)}
    return view


def clear() -> None:
    with state.lock:
        if access.preserve_history():
            state.data["people"].pop(config.OWNER_ADDRESS, None)
        else:
            state.data["people"] = {}
    state.save()

