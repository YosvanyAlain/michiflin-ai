"""Useful inline answers with private, bounded, on-demand continuation.

There are no files or extra model calls. When an unusually long answer will not
fit the comfortable app/history budget, retain its exact text temporarily and
show the next portion only when this same verified app asks for it.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from .util import tr

# The installed app includes at most 6,000 JavaScript UTF-16 units per prior
# turn. Leave space for code fences and the notice, including astral emoji.
CONTENT_UNITS = 4800
MAX_STORED_CHARS = 100_000
MAX_TOTAL_CHARS = 1_000_000
MAX_ENTRIES = 100
TTL = 1800.0

_CONTINUE = re.compile(
    r"^(?:(?:por favor|please)[,\s]+)?"
    r"(?:(?:contin[uú][ae]|continuar|sigue|seguir|prosigue)"
    r"(?:\s+(?:con\s+)?(?:la respuesta|la explicaci[oó]n|el c[oó]digo|lo anterior|la siguiente parte|el siguiente punto))?"
    r"|m[aá]s|m[aá]s detalles|cu[eé]ntame m[aá]s"
    r"|(?:continue|go on)(?:\s+(?:with\s+)?(?:the answer|the explanation|the code|the next part))?"
    r"|more|more details|tell me more)"
    r"(?:[,\s]+(?:por favor|please))?[\s?!.¿¡]*$", re.I,
)
_FENCE = re.compile(r"```([\w-]*)\n?", re.ASCII)  # matches the installed app's rich() parser
_SENTENCE = re.compile(r"[.!?。！？](?:\s+|$)")


def is_continue(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONTINUE.fullmatch(value.strip()))


def is_explicit_continue(value: Any) -> bool:
    if not is_continue(value):
        return False
    return bool(re.search(r"(?:contin[uú][ae]|continuar|sigue|seguir|prosigue|continue|go on)", value, re.I))


def _units(text: str) -> int:
    return sum(2 if ord(ch) > 0xffff else 1 for ch in text)


def _end(text: str, start: int) -> int:
    units, end = 0, start
    while end < len(text):
        size = 2 if ord(text[end]) > 0xffff else 1
        if units + size > CONTENT_UNITS:
            break
        units += size
        end += 1
    if end == len(text):
        return end
    window = text[start:end]
    floor = len(window) * 2 // 3
    # Favor complete paragraphs, then complete lines or sentences, then a word
    # boundary. For a very long token/URL, preserve it across a hard split.
    for separator in ("\n\n", "\n"):
        index = window.rfind(separator, floor)
        if index >= floor:
            return start + index + len(separator)
    matches = list(_SENTENCE.finditer(window, floor))
    if matches:
        return start + matches[-1].end()
    index = max(window.rfind(" ", floor), window.rfind("\t", floor))
    if index >= floor:
        return start + index + 1
    # Do not split a Markdown fence token across pages.
    while end > start + 1 and text[end - 1] == "`" and end < len(text) and text[end] == "`":
        end -= 1
    return end


def _open_fence(text: str, end: int) -> str | None:
    language: str | None = None
    for match in _FENCE.finditer(text, 0, end):
        language = match.group(1)[:32] if language is None else None
    return language


def _page(text: str, start: int, locale: str, truncated: bool) -> tuple[str, int]:
    end = _end(text, start)
    before, after = _open_fence(text, start), _open_fence(text, end)
    prefix = ("```" + before + "\n") if before is not None else ""
    part = prefix + text[start:end]
    if end < len(text):
        if after is not None:
            part += "\n```"
        note = tr(locale,
                  "Hay más de esta respuesta. Escribe «continúa» para seguir.",
                  "There is more to this answer. Type ‘continue’ to keep reading.")
        if truncated:
            note = tr(locale,
                      "El modelo alcanzó su límite; queda texto generado. Escribe «continúa» para verlo.",
                      "The model reached its limit; generated text remains. Type ‘continue’ to read it.")
        part += "\n\n" + note
    # The client sends each past assistant message in at most 6,000 UTF-16
    # units. Its exact visible content is used only as a receipt, not as a
    # trusted model instruction or a way to access another person's data.
    assert _units(part) < 6000
    return part, end


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _last_assistant(history: Any) -> str:
    if isinstance(history, list):
        for turn in reversed(history[-12:]):
            if isinstance(turn, (list, tuple)) and len(turn) == 2 and turn[0] == "assistant" and isinstance(turn[1], str):
                return turn[1]
    return ""


@dataclass
class _PageState:
    text: str
    offset: int
    last: str
    signature: str
    truncated: bool
    at: float


class InlinePages:
    """RAM only; isolated by instance, verified address, grant and mode.

    The visible last page acts as a receipt: a repeated request with stale app
    history repeats the same page instead of skipping unseen text. Entries are
    never evicted to make room for another person; on pressure the full answer
    is sent inline instead of silently losing part of it.
    """
    def __init__(self, ttl: float = TTL, maximum: int = MAX_ENTRIES, total: int = MAX_TOTAL_CHARS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.maximum, self.total, self.clock = ttl, maximum, total, clock
        self._items: OrderedDict[tuple[int, str, str], _PageState] = OrderedDict()
        # Small, text-free tombstones prevent silently continuing beyond unseen
        # text when the remaining pages have expired during the same session.
        self._expired: OrderedDict[tuple[int, str, str], tuple[float, str, bool]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(instance: int, address: str, scope: str) -> tuple[int, str, str]:
        return int(instance), str(address or "").strip().casefold(), str(scope or "")

    def _prune(self, now: float) -> None:
        for key, item in list(self._items.items()):
            if now - item.at > self.ttl:
                self._items.pop(key)
                self._expired[key] = (now, _digest(item.last), item.offset == len(item.text))
        for key, (at, _hash, _finished) in list(self._expired.items()):
            if now - at > self.ttl:
                self._expired.pop(key, None)
        while len(self._expired) > self.maximum * 2:
            self._expired.popitem(last=False)

    def prune(self) -> None:
        """Drop expired text even if no one requests another page."""
        with self._lock:
            self._prune(self.clock())

    def forget(self, address: str) -> None:
        """Forget all pending pages for the person requesting a reset."""
        person = (address or "").strip().casefold()
        if not person:
            return
        with self._lock:
            for key in list(self._items):
                if key[1] == person:
                    self._items.pop(key, None)
            for key in list(self._expired):
                if key[1] == person:
                    self._expired.pop(key, None)

    def start(self, key: tuple[int, str, str], answer: Any, locale: str) -> tuple[Any, bool]:
        """Begin a new response, or return it unchanged if storage is unsafe."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._items.pop(key, None)
            self._expired.pop(key, None)
            if not key[1] or not isinstance(answer, dict) or answer.get("files") or answer.get("error"):
                return answer, False
            text = answer.get("text")
            if (not isinstance(text, str) or not text or text.lstrip().startswith(("⚠️", "❌", "🛑"))
                    or _units(text) <= CONTENT_UNITS or len(text) > MAX_STORED_CHARS):
                return answer, False
            first, end = _page(text, 0, locale, bool(answer.get("truncated")))
            if end >= len(text):
                return answer, False
            used = sum(len(item.text) + len(item.last) for item in self._items.values())
            if len(self._items) >= self.maximum or used + len(text) + len(first) > self.total:
                return answer, False
            self._items[key] = _PageState(text, end, first, str(answer.get("sig") or ""),
                                          bool(answer.get("truncated")), now)
            result = dict(answer)
            result["text"] = first
            return result, True

    def status(self, key: tuple[int, str, str], history: Any) -> str:
        """Return ready, expired or none; this never consumes a pending page."""
        with self._lock:
            self._prune(self.clock())
            last = _last_assistant(history)
            item = self._items.get(key)
            if item:
                if item.offset == len(item.text) and last == item.last:
                    self._items.pop(key, None)  # the final page was seen
                    return "none"
                return "ready"
            expired = self._expired.get(key)
            if expired:
                if expired[2] and last and _digest(last) == expired[1]:
                    self._expired.pop(key, None)  # complete answer was seen
                    return "none"
                return "expired"
            return "none"

    def take(self, key: tuple[int, str, str], history: Any, locale: str) -> dict[str, Any] | None:
        """Return one page, repeat an unacknowledged page, or a truthful expiry."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            item = self._items.get(key)
            if item is None:
                if key in self._expired:
                    return {"text": tr(locale,
                        "La parte pendiente ya no está disponible. Recuérdame el tema o copia el último fragmento para seguir.",
                        "The remaining part is no longer available. Remind me of the topic or paste the last part to continue.")}
                return None
            if _last_assistant(history) == item.last and item.offset < len(item.text):
                item.last, item.offset = _page(item.text, item.offset, locale, item.truncated)
            item.at = now
            result: dict[str, Any] = {"text": item.last, "sig": item.signature}
            if item.truncated:
                result["truncated"] = True
            return result
