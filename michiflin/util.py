"""Small shared helpers: localization, formatting, text cleanup."""
from __future__ import annotations

import html
import re
import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from .config import LOCAL_ZONE, MIB

SPANISH_WORDS = {
    "hola", "que", "qué", "como", "cómo", "para", "gracias", "por", "favor", "archivo",
    "clima", "noticias", "descargar", "enviar", "ayuda", "quiero", "puedes", "tengo",
    "con", "una", "un", "el", "la", "los", "las", "del", "de", "es", "y", "en", "dame",
    "hazme", "dime", "sobre", "cual", "cuál", "imagen", "foto", "busca", "buscar",
}
ENGLISH_WORDS = {
    "hello", "please", "what", "how", "download", "weather", "news", "file", "help",
    "thanks", "can", "you", "the", "and", "with", "from", "about", "is", "are", "give",
    "make", "tell", "which", "image", "picture", "search", "find", "draw",
}

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)


def tr(locale: str, spanish: str, english: str) -> str:
    return english if locale == "en" else spanish


def language_signal(text: str) -> str:
    """'es' or 'en' when the text leans clearly to one language, else ''."""
    words = re.findall(r"[a-záéíóúüñ]+", text.lower())
    spanish = sum(word in SPANISH_WORDS for word in words)
    english = sum(word in ENGLISH_WORDS for word in words)
    if re.search(r"[áéíóúüñ¿¡]", text.lower()):
        spanish += 2
    return "en" if english > spanish else "es" if spanish > english else ""


def compact(value: Any, limit: int = 260) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()[:limit]


def clean_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[\t \f\v]+", " ", value)
    value = re.sub(r"\n[ ]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def human_size(size: int | float | None) -> str:
    if not size or size <= 0:
        return "?"
    if size < 1024:
        return f"{int(size)} B"
    if size < MIB:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * MIB:
        return f"{size / MIB:.1f} MB"
    return f"{size / (1024 * MIB):.2f} GB"


def duration_text(seconds: int | float | None) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def span_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"


def local_time(timestamp: float | None, fmt: str = "%d/%m %H:%M") -> str:
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(timestamp, LOCAL_ZONE).strftime(fmt)


def local_today() -> str:
    return datetime.now(LOCAL_ZONE).strftime("%Y-%m-%d")


def utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def clean_filename(value: str, fallback: str = "file") -> str:
    value = unquote(value or "").replace("\\", "_").replace("/", "_")
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[^\w.()\[\] -]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip(" ._")
    if len(value) > 120:
        stem, dot, ext = value.rpartition(".")
        value = (stem[: 110] + "." + ext[:8]) if dot and len(ext) <= 8 else value[:120]
    return value or fallback


def first_url(text: str) -> str:
    match = URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?)]}»\"'")


def is_url(text: str) -> bool:
    """True when the text looks like an absolute HTTP(S) URL and nothing else."""
    value = (text or "").strip()
    if not value or " " in value:
        return False
    return bool(re.match(r"^https?://[^\s<>\"'`]+$", value, re.I))


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def unlinked(value: str) -> str:
    """Render a domain so chat clients do not turn it into a browser link."""
    return value.replace(".", "\u2024")


def strip_thinking(text: str) -> str:
    return re.sub(r"(?is)<think>.*?</think>", "", text or "").strip()
