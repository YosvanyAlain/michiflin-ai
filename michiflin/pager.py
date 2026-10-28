"""Paged lists: the next/back commands move through the last list sent in a chat."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from . import chat
from .util import tr


@dataclass
class Pager:
    title: str
    pages: list[str]
    locale: str = "es"
    kind: str = ""
    tail: str = ""
    index: int = 0
    data: dict = field(default_factory=dict)

    def has_next(self) -> bool:
        return self.index < len(self.pages) - 1


_pagers: dict[int, Pager] = {}
_lock = threading.Lock()


def chunk(entries: list[str], size: int) -> list[str]:
    return ["\n".join(entries[start:start + size]) for start in range(0, len(entries), size)] or [""]


def nav_line(pager: Pager) -> str:
    back = tr(pager.locale, "⬅️ /anterior", "⬅️ /back")
    ahead = tr(pager.locale, "/siguiente ➡️", "/next ➡️")
    if len(pager.pages) < 2:
        return ""
    if pager.index == 0:
        return ahead
    if not pager.has_next():
        return back
    return f"{back}      {ahead}"


def render(pager: Pager) -> str:
    total = len(pager.pages)
    head = f"{pager.title} · {pager.index + 1}/{total}" if total > 1 else pager.title
    parts = [head, "", pager.pages[pager.index].strip()]
    nav = nav_line(pager)
    if pager.tail:
        parts += ["", pager.tail]
    if nav:
        parts += ["", nav]
    return "\n".join(parts)


def show(chat_id: int, pager: Pager) -> str:
    """Remember the list for navigation and return the current page text."""
    pager.index = max(0, min(pager.index, len(pager.pages) - 1))
    with _lock:
        _pagers[chat_id] = pager
    return render(pager)


def current(chat_id: int) -> Pager | None:
    with _lock:
        return _pagers.get(chat_id)


def move(chat_id: int, step: int, locale: str) -> str:
    pager = current(chat_id)
    if pager is None:
        return tr(locale, "ℹ️ No hay una lista abierta. Prueba /buscar, /videos o /ayuda.",
                  "ℹ️ No list is open. Try /search, /videos or /help.")
    target = pager.index + step
    if not 0 <= target < len(pager.pages):
        return tr(locale, "ℹ️ Ya estás en la última página.", "ℹ️ This is the last page.") if step > 0 else \
            tr(locale, "ℹ️ Ya estás en la primera página.", "ℹ️ This is the first page.")
    pager.index = target
    return render(pager)


def jump(chat_id: int, number: int, locale: str) -> str:
    pager = current(chat_id)
    if pager is None:
        return move(chat_id, 0, locale)
    return move(chat_id, number - 1 - pager.index, locale)


def send(chat_id: int, pager: Pager) -> int:
    return chat.text(chat_id, show(chat_id, pager))
