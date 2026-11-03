"""Offline, inert HTML for long answers and a small, private follow-up cache.

The existing WebXDC is deliberately unchanged. Its visible answer becomes a
preview after (and only after) Delta Chat has accepted the attachment locally.
"""
from __future__ import annotations

from collections import OrderedDict
import html
import re
import threading
import time
from typing import Any, Callable

from .util import tr

LONG_CHARS = 1200
LONG_LINES = 20
MAX_TEXT_CHARS = 30_000
MAX_DOCUMENT_BYTES = 180_000
HISTORY_TEXT_CHARS = 6000  # The installed miniapp already sends at most this much.

_INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*)")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.+)$")
_NUMBER = re.compile(r"^\s*(\d{1,4})[.)]\s+(.+)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})([^`]*)$")


def is_long(text: Any) -> bool:
    return isinstance(text, str) and (len(text) >= LONG_CHARS or text.count("\n") >= LONG_LINES - 1)


def _inline(text: str) -> str:
    """Support only code and bold; every piece, including URL text, is escaped."""
    out: list[str] = []
    end = 0
    for match in _INLINE.finditer(text):
        out.append(html.escape(text[end:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            out.append("<code>" + html.escape(token[1:-1]) + "</code>")
        else:
            out.append("<strong>" + html.escape(token[2:-2]) + "</strong>")
        end = match.end()
    out.append(html.escape(text[end:]))
    return "".join(out)


def _body(text: str) -> str:
    """A deliberately small Markdown subset; no HTML or active links are parsed."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    listing = ""
    code: list[str] | None = None
    fence = ""

    def end_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def end_list() -> None:
        nonlocal listing
        if listing:
            out.append(f"</{listing}>")
            listing = ""

    for line in lines:
        if code is not None:
            # A closing fence must use the same character and at least the
            # opening length; content inside is never interpreted as HTML.
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                out.append("<pre><code>" + html.escape("\n".join(code) + ("\n" if code else "")) + "</code></pre>")
                code, fence = None, ""
            else:
                code.append(line)
            continue
        marker = _FENCE.match(line)
        if marker:
            end_paragraph()
            end_list()
            fence, code = marker.group(1), []
            label = marker.group(2).strip()
            if label:
                out.append('<div class="code-label">' + html.escape(label) + "</div>")
            continue
        if not line.strip():
            end_paragraph()
            end_list()
            continue
        heading = _HEADING.match(line)
        if heading:
            end_paragraph()
            end_list()
            level = min(4, len(heading.group(1)) + 1)
            out.append(f"<h{level}>" + _inline(heading.group(2)) + f"</h{level}>")
            continue
        bullet, numbered = _BULLET.match(line), _NUMBER.match(line)
        if bullet or numbered:
            end_paragraph()
            kind = "ul" if bullet else "ol"
            if listing != kind:
                end_list()
                out.append(f"<{kind}>")
                listing = kind
            if bullet:
                out.append("<li>" + _inline(bullet.group(1)) + "</li>")
            elif numbered:
                out.append(f'<li value="{int(numbered.group(1))}">' + _inline(numbered.group(2)) + "</li>")
            continue
        end_list()
        paragraph.append(line)
    if code is not None:
        # An unfinished model response can end in the middle of a code fence.
        # Keep all generated code visible rather than dropping the last block.
        out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
    end_paragraph()
    end_list()
    return "\n".join(out)


def render(text: str, locale: str, *, signature: str = "", truncated: bool = False) -> str:
    """Create a bounded standalone page, or raise ValueError without truncating it."""
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ValueError("answer_too_large")
    lang = "en" if locale == "en" else "es"
    title = tr(lang, "Tu respuesta", "Your answer")
    subtitle = tr(lang, "Una lectura clara, también sin conexión", "A clear read, even offline")
    attribution = ""
    if signature:
        if len(str(signature)) > 5000:
            raise ValueError("signature_too_large")
        attribution = '<div class="attribution">' + html.escape(tr(lang, "Origen", "Source")) + " · " + html.escape(str(signature)) + "</div>"
    notice = ""
    if truncated:
        notice_text = tr(lang, "La generación se cortó por el límite del modelo. Este archivo contiene lo que sí se generó.",
                         "Generation stopped at the model's limit. This file contains what was generated.")
        notice = '<div class="notice" role="note">' + html.escape(notice_text) + "</div>"
    footer = tr(lang, "Preparado por Michiflin · Disponible sin conexión", "Prepared by Michiflin · Available offline")
    document = f'''<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
<title>{html.escape(title)} · Michiflin</title><style>
:root{{color-scheme:light dark;--bg:#f3f5f8;--paper:#fff;--text:#202a3a;--muted:#66758a;--line:#e3e9f0;--brand:#405ca8;--soft:#eaf0ff;--code:#f3f6fa;--warn:#fff5df;--warn-text:#805313}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.72 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow-wrap:anywhere}}
main{{max-width:820px;margin:34px auto;padding:0 18px}}.paper{{background:var(--paper);border:1px solid var(--line);border-radius:20px;box-shadow:0 16px 48px #1c2e4b0c;padding:clamp(22px,5vw,48px)}}
header{{display:flex;align-items:flex-start;gap:15px;padding-bottom:24px;border-bottom:1px solid var(--line)}}.mark{{width:43px;height:43px;flex:none;border-radius:14px;background:var(--soft);color:var(--brand);display:grid;place-items:center;font-weight:800;font-size:21px}}
.eyebrow{{margin:0 0 2px;color:var(--brand);font-size:11px;font-weight:750;letter-spacing:.14em;text-transform:uppercase}}h1{{font-size:clamp(25px,5vw,33px);line-height:1.2;letter-spacing:-.025em;margin:0}}.subtitle{{margin:5px 0 0;color:var(--muted);font-size:14px}}
article{{padding-top:20px}}article p{{margin:0 0 1.05em;white-space:pre-wrap}}article h2,article h3,article h4{{line-height:1.3;letter-spacing:-.015em;margin:1.35em 0 .55em}}article h2{{font-size:1.35em}}article h3{{font-size:1.14em}}article h4{{font-size:1em}}ul,ol{{padding-left:1.35em;margin:.5em 0 1.15em}}li{{padding-left:.15em;margin:.28em 0}}
code{{font:0.88em/1.6 ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--code);border-radius:5px;padding:.1em .3em}}.code-label{{margin:.9em 0 -.6em;color:var(--muted);font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace}}pre{{margin:1em 0 1.35em;overflow:auto;max-width:100%;background:var(--code);border:1px solid var(--line);border-radius:12px;padding:15px;white-space:pre;overflow-wrap:normal}}pre code{{padding:0;background:none;font-size:13px}}
.notice{{margin:18px 0 0;border-left:3px solid #dc9b28;background:var(--warn);color:var(--warn-text);border-radius:4px 10px 10px 4px;padding:11px 14px;font-size:14px}}.attribution{{margin-top:22px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}}footer{{text-align:center;padding:18px 4px 4px;color:var(--muted);font-size:12px}}
@media(max-width:480px){{main{{margin:14px auto;padding:0 10px}}.paper{{border-radius:15px}}body{{font-size:15px}}header{{gap:11px}}}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111824;--paper:#192332;--text:#e8edf5;--muted:#a3b1c3;--line:#303e50;--brand:#afc3ff;--soft:#2a3c61;--code:#111b29;--warn:#45341c;--warn-text:#f3d296}}.paper{{box-shadow:none}}}}
@media print{{body{{background:#fff;color:#111}}main{{margin:0;max-width:none;padding:0}}.paper{{border:0;box-shadow:none;padding:0}}}}
</style></head><body><main><div class="paper"><header><div class="mark" aria-hidden="true">M</div><div><p class="eyebrow">Michiflin</p><h1>{html.escape(title)}</h1><p class="subtitle">{html.escape(subtitle)}</p></div></header>{notice}<article>{_body(text)}</article>{attribution}</div><footer>{html.escape(footer)}</footer></main></body></html>'''
    if len(document.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise ValueError("document_too_large")
    return document


def preview(text: str, locale: str, *, truncated: bool = False) -> str:
    """Short, truthful preview: locally accepted is not the same as delivered."""
    excerpt = text[:350].strip()
    fences = list(re.finditer(r"(?m)^\s*(?:`{3,}|~{3,})[^\n]*$", excerpt))
    if len(fences) % 2:
        # Do not leave the miniapp's Markdown renderer inside an open code
        # block, which would swallow the file notice into the code itself.
        excerpt = excerpt[:fences[-1].start()].strip()
        code_note = tr(locale, "El código continúa en el archivo.", "The code continues in the file.")
        excerpt = (excerpt + "\n\n" if excerpt else "") + code_note
    elif len(text) > len(excerpt):
        excerpt = excerpt.rstrip() + "…"
    notice = tr(locale, "Archivo HTML enviado al chat; puede tardar en llegar.",
                "HTML file sent to the chat; it may take a moment to arrive.")
    if truncated:
        notice += " " + tr(locale, "Contiene lo generado; la respuesta quedó cortada.",
                            "It contains what was generated; the answer was cut short.")
    return excerpt + "\n\n" + notice


class PreviewHistory:
    """Restore recent full answers for follow-ups, scoped to an app and sender.

    This is process-only, bounded, and expires; it never changes the installed
    app or persisted conversations. Ambiguous identical previews are not used.
    """

    def __init__(self, ttl: float = 1800, maximum: int = 128, per_instance: int = 6,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.maximum, self.per_instance, self.clock = ttl, maximum, per_instance, clock
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[int, str, str, str], tuple[float, str | None]] = OrderedDict()

    def _prune(self, now: float) -> None:
        for key, (created, _text) in list(self._items.items()):
            if now - created >= self.ttl:
                del self._items[key]
        while len(self._items) > self.maximum:
            self._items.popitem(last=False)

    def remember(self, instance: int, sender: str, space: str, short: str, full: str) -> None:
        key = (instance, sender.casefold(), space[:180], short)
        value = (full if len(full) <= HISTORY_TEXT_CHARS else
                 full[:2700] + "\n[…]\n" + full[-3295:])
        now = self.clock()
        with self._lock:
            self._prune(now)
            previous = self._items.get(key)
            # Never substitute the wrong full answer when two share a preview.
            if previous is not None and previous[1] != value:
                value = None
            self._items[key] = (now, value)
            self._items.move_to_end(key)
            same = [item for item in self._items if item[:3] == key[:3]]
            for item in same[:-self.per_instance]:
                del self._items[item]
            self._prune(now)

    def expand(self, instance: int, sender: str, space: str, history: Any) -> Any:
        if not isinstance(history, list):
            return history
        now = self.clock()
        with self._lock:
            self._prune(now)
            values = {key[3]: item[1] for key, item in self._items.items()
                      if key[:3] == (instance, sender.casefold(), space[:180]) and item[1] is not None}
        changed = False
        restored: list[Any] = []
        for turn in history:
            if (isinstance(turn, list) and len(turn) == 2 and turn[0] == "assistant"
                    and isinstance(turn[1], str) and turn[1] in values):
                restored.append([turn[0], values[turn[1]]])
                changed = True
            else:
                restored.append(turn)
        return restored if changed else history
