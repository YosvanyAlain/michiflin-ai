"""Quota-free utilities: translation, unit conversion, local time and video subtitles as text."""
from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import requests

from . import config, names
from .util import compact, tr

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

# ---------------------------------------------------------------------------
# Translation.
# ---------------------------------------------------------------------------
LANGUAGES = {
    "es": ("español", "Spanish"), "en": ("inglés", "English"), "fr": ("francés", "French"), "pt": ("portugués", "Portuguese"),
    "it": ("italiano", "Italian"), "de": ("alemán", "German"), "ru": ("ruso", "Russian"), "zh-CN": ("chino", "Chinese"),
    "ja": ("japonés", "Japanese"), "ko": ("coreano", "Korean"), "ar": ("árabe", "Arabic"), "nl": ("neerlandés", "Dutch"),
    "pl": ("polaco", "Polish"), "tr": ("turco", "Turkish"), "uk": ("ucraniano", "Ukrainian"), "hi": ("hindi", "Hindi"),
    "sv": ("sueco", "Swedish"), "el": ("griego", "Greek"), "he": ("hebreo", "Hebrew"), "ca": ("catalán", "Catalan"),
    "ro": ("rumano", "Romanian"), "vi": ("vietnamita", "Vietnamese"), "id": ("indonesio", "Indonesian"),
}
_LANGUAGE_WORDS = {
    "español": "es", "espanol": "es", "spanish": "es", "es": "es", "inglés": "en", "ingles": "en", "english": "en", "en": "en",
    "francés": "fr", "frances": "fr", "french": "fr", "fr": "fr", "portugués": "pt", "portugues": "pt", "portuguese": "pt", "pt": "pt",
    "italiano": "it", "italian": "it", "it": "it", "alemán": "de", "aleman": "de", "german": "de", "de": "de",
    "ruso": "ru", "russian": "ru", "ru": "ru", "chino": "zh-CN", "chinese": "zh-CN", "zh": "zh-CN",
    "japonés": "ja", "japones": "ja", "japanese": "ja", "ja": "ja", "coreano": "ko", "korean": "ko", "ko": "ko",
    "árabe": "ar", "arabe": "ar", "arabic": "ar", "ar": "ar", "neerlandés": "nl", "holandés": "nl", "dutch": "nl", "nl": "nl",
    "polaco": "pl", "polish": "pl", "pl": "pl", "turco": "tr", "turkish": "tr", "tr": "tr",
    "ucraniano": "uk", "ukrainian": "uk", "uk": "uk", "hindi": "hi", "hi": "hi",
}


def language_code(word: str) -> str:
    return _LANGUAGE_WORDS.get(word.strip().lower().strip(":,."), "")


def language_name(code: str, locale: str) -> str:
    pair = LANGUAGES.get(code)
    return (pair[1] if locale == "en" else pair[0]) if pair else code


def _translation_chunks(text: str, max_bytes: int) -> list[tuple[str, str]]:
    """Split at whitespace without dropping content; keep separators outside translations.

    Providers limit UTF-8 bytes, not necessarily Unicode characters. Never
    silently return a translated prefix as if it was the entire text.
    """
    parts: list[tuple[str, str]] = []
    rest = text
    while rest:
        used = 0
        end = 0
        for char in rest:
            size = len(char.encode("utf-8"))
            if used + size > max_bytes:
                break
            used += size
            end += 1
        if end == len(rest):
            parts.append((rest, ""))
            break
        if not end:
            raise ValueError("segment")
        # Prefer a natural boundary, but retain a long word rather than truncating it.
        boundary = max(rest.rfind("\n", 0, end), rest.rfind(" ", 0, end))
        if boundary > end // 3:
            end = boundary
        segment = rest[:end]
        tail = rest[end:]
        sep_match = re.match(r"\s+", tail)
        separator = sep_match.group() if sep_match else ""
        parts.append((segment, separator))
        rest = tail[len(separator):]
    return parts


def translate_detailed(text: str, target: str, source: str = "auto") -> dict[str, str]:
    """Translate without AI-provider quota; output is all-or-nothing.

    Public services are external, may impose their own limits, and receive the text.
    The optional LibreTranslate fallback is an operator-configured HTTPS API.
    Never silently guess a source language or truncate text.
    """
    if not text or len(text) > 4500 or target not in LANGUAGES or (source != "auto" and source not in LANGUAGES):
        return {"text": "", "detected": source, "provider": ""}
    if source == target:
        return {"text": text, "detected": source, "provider": "identity"}
    try:
        translated, detected = [], source
        for segment, separator in _translation_chunks(text, 3500):
            state_check_translation()
            response = requests.get("https://translate.googleapis.com/translate_a/single",
                                    params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": segment}, headers=UA, timeout=15)
            response.raise_for_status()
            data = response.json()
            part = "".join(str(item[0]) for item in data[0] if item and item[0])
            if not part:
                raise ValueError("empty translation")
            if source == "auto" and len(data) > 2 and data[2]:
                # Keep the first detected language; subsequent chunks may differ.
                if detected == "auto":
                    detected = str(data[2])
            translated.append(part + separator)
        return {"text": "".join(translated), "detected": detected, "provider": "Google Translate"}
    except (requests.RequestException, ValueError, IndexError, TypeError, KeyError):
        pass
    # Public translation-memory matches are not reliably machine translations:
    # an exact match can contain vandalized or unrelated text. Do not silently
    # present those as a fallback. An operator may configure LibreTranslate.
    endpoint = config.LIBRETRANSLATE_URL.strip()
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid translation endpoint")
        translated, detected = [], source
        for segment, separator in _translation_chunks(text, 3500):
            state_check_translation()
            body = {"q": segment, "source": source, "target": target, "format": "text"}
            if config.LIBRETRANSLATE_API_KEY:
                body["api_key"] = config.LIBRETRANSLATE_API_KEY
            response = requests.post(endpoint, json=body, timeout=25, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("translation unavailable")
            data = response.json()
            part = str(data.get("translatedText") or "")
            if not part:
                raise ValueError("translation unavailable")
            if source == "auto" and detected == "auto":
                detected = str((data.get("detectedLanguage") or {}).get("language") or "auto")
            translated.append(part + separator)
        return {"text": "".join(translated), "detected": detected, "provider": "LibreTranslate"}
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return {"text": "", "detected": source, "provider": ""}


def state_check_translation() -> None:
    # Respect maintenance and the user's kill switch between chunks.
    from . import state
    state.check_work_allowed()


def translate(text: str, target: str, source: str = "auto") -> tuple[str, str]:
    """Compatibility wrapper for chat commands and old miniapp instances."""
    result = translate_detailed(text, target, source)
    return result["text"], result["detected"]


def translate_text(argument: str, locale: str) -> str:
    """'[language] text': the target defaults to Spanish, or English when the text is already Spanish."""
    words = argument.strip().split(maxsplit=1)
    if not words:
        return tr(locale, f"Uso: {names.cmd(locale, 'translate')} [idioma] texto · ej. {names.cmd(locale, 'translate')} inglés buenos días",
                  f"Usage: {names.cmd(locale, 'translate')} [language] text · e.g. {names.cmd(locale, 'translate')} spanish good morning")
    target = language_code(words[0]) if len(words) == 2 else ""
    text = words[1] if target else argument.strip()
    if len(text) > 4500:
        return tr(locale, "⚠️ El texto supera 4500 caracteres; divídelo antes de traducir.",
                  "⚠️ Text exceeds 4500 characters; split it before translating.")
    translated, detected = translate(text, target or "es")
    if translated and not target and detected.startswith("es"):
        target = "en"
        translated, detected = translate(text, "en")
    target = target or "es"
    if not translated:
        return tr(locale, "⚠️ Los traductores no respondieron. Prueba más tarde.", "⚠️ The translators did not answer. Try again later.")
    return f"🌐 {language_name(detected, locale)} → {language_name(target, locale)}\n\n{translated}"


# ---------------------------------------------------------------------------
# Unit conversion.
# ---------------------------------------------------------------------------
# unit -> (dimension, factor to the base unit)
UNITS: dict[str, tuple[str, float]] = {
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "in": ("length", 0.0254), "pulgada": ("length", 0.0254), "pulgadas": ("length", 0.0254), "inch": ("length", 0.0254), "inches": ("length", 0.0254),
    "ft": ("length", 0.3048), "pie": ("length", 0.3048), "pies": ("length", 0.3048), "foot": ("length", 0.3048), "feet": ("length", 0.3048),
    "yd": ("length", 0.9144), "yarda": ("length", 0.9144), "yardas": ("length", 0.9144), "yard": ("length", 0.9144),
    "mi": ("length", 1609.344), "milla": ("length", 1609.344), "millas": ("length", 1609.344), "mile": ("length", 1609.344), "miles": ("length", 1609.344),
    "mg": ("mass", 0.001), "g": ("mass", 1.0), "kg": ("mass", 1000.0), "t": ("mass", 1_000_000.0),
    "lb": ("mass", 453.59237), "lbs": ("mass", 453.59237), "libra": ("mass", 453.59237), "libras": ("mass", 453.59237), "pound": ("mass", 453.59237), "pounds": ("mass", 453.59237),
    "oz": ("mass", 28.349523125), "onza": ("mass", 28.349523125), "onzas": ("mass", 28.349523125), "ounce": ("mass", 28.349523125),
    "ml": ("volume", 0.001), "l": ("volume", 1.0), "litro": ("volume", 1.0), "litros": ("volume", 1.0), "liter": ("volume", 1.0), "liters": ("volume", 1.0),
    "gal": ("volume", 3.785411784), "galon": ("volume", 3.785411784), "galón": ("volume", 3.785411784), "galones": ("volume", 3.785411784), "gallon": ("volume", 3.785411784),
    "kmh": ("speed", 1 / 3.6), "km/h": ("speed", 1 / 3.6), "mph": ("speed", 0.44704), "m/s": ("speed", 1.0), "nudo": ("speed", 0.514444), "nudos": ("speed", 0.514444), "knot": ("speed", 0.514444),
    "b": ("data", 1.0), "kb": ("data", 1024.0), "mb": ("data", 1024.0 ** 2), "gb": ("data", 1024.0 ** 3), "tb": ("data", 1024.0 ** 4),
    "m2": ("area", 1.0), "km2": ("area", 1e6), "ha": ("area", 10_000.0), "hectarea": ("area", 10_000.0), "hectárea": ("area", 10_000.0), "acre": ("area", 4046.8564224),
    "caballeria": ("area", 134_202.0), "caballería": ("area", 134_202.0), "caballerias": ("area", 134_202.0),
}
TEMPERATURES = {"c": "c", "°c": "c", "celsius": "c", "f": "f", "°f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}
_CONVERT = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*([^\s\d]+)\s+(?:a|en|to|in)\s+([^\s]+)\s*$", re.I)


def convert(value: float, source: str, target: str) -> float | None:
    source, target = source.lower(), target.lower()
    if source in TEMPERATURES and target in TEMPERATURES:
        a, b = TEMPERATURES[source], TEMPERATURES[target]
        celsius = value if a == "c" else (value - 32) * 5 / 9 if a == "f" else value - 273.15
        return celsius if b == "c" else celsius * 9 / 5 + 32 if b == "f" else celsius + 273.15
    first, second = UNITS.get(source), UNITS.get(target)
    if not first or not second or first[0] != second[0]:
        return None
    return value * first[1] / second[1]


def convert_text(argument: str, locale: str) -> str:
    match = _CONVERT.match(argument)
    usage = tr(locale, f"Uso: {names.cmd(locale, 'convert')} 10 km a mi · 30 c a f · 5 lb a kg · 2 gb a mb",
               f"Usage: {names.cmd(locale, 'convert')} 10 km to mi · 30 c to f · 5 lb to kg · 2 gb to mb")
    if not match:
        return usage
    value = float(match.group(1).replace(",", "."))
    result = convert(value, match.group(2), match.group(3))
    if result is None:
        return tr(locale, "⚠️ No sé convertir entre esas unidades.\n", "⚠️ I can't convert between those units.\n") + usage
    shown = f"{result:,.4f}".rstrip("0").rstrip(".")
    if locale != "en":
        shown = shown.replace(",", " ").replace(".", ",")
    return f"📏 {match.group(1)} {match.group(2)} = {shown} {match.group(3)}"


# ---------------------------------------------------------------------------
# Local time.
# ---------------------------------------------------------------------------
def time_text(place: str, locale: str) -> str:
    place = place.strip()
    if not place:
        return tr(locale, f"Uso: {names.cmd(locale, 'time')} ciudad", f"Usage: {names.cmd(locale, 'time')} city")
    try:
        response = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                                params={"name": place, "count": 1, "language": locale, "format": "json"}, timeout=20)
        response.raise_for_status()
        found = (response.json().get("results") or [None])[0]
    except (requests.RequestException, ValueError):
        found = None
    if not found or not found.get("timezone"):
        return tr(locale, f"🕒 No encontré “{place}”.", f"🕒 “{place}” was not found.")
    now = datetime.now(ZoneInfo(found["timezone"]))
    where = ", ".join(bit for bit in (found.get("name"), found.get("country")) if bit)
    offset = now.strftime("%z")
    return f"🕒 {where}\n{now.strftime('%H:%M · %d/%m/%Y')} (UTC{offset[:3]}:{offset[3:]})"


# ---------------------------------------------------------------------------
# Video subtitles as plain text: a few KB instead of megabytes of video.
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")


def _vtt_text(raw: str) -> str:
    lines, last = [], ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit() or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = compact(_TAG.sub("", line), 500)
        if line and line != last:
            lines.append(line)
            last = line
    text = " ".join(lines)
    return re.sub(r"(?<=[.!?])\s+", "\n", text)


def _pick_track(info: dict, preferred: list[str]) -> tuple[str, bool]:
    """Best subtitle language: manual tracks first, then original-language automatic captions."""
    manual, automatic = info.get("subtitles") or {}, info.get("automatic_captions") or {}
    for code in preferred:
        if code in manual:
            return code, False
    original = str(info.get("language") or "")
    for code in [f"{original}-orig", original] + preferred:
        if code and code in automatic:
            return code, True
    return "", False


def transcript(url: str, language: str) -> tuple[str, str]:
    """Return (title, text) of one subtitle track; raises ValueError('no_subtitles') when absent."""
    from . import media

    preferred = list(dict.fromkeys([language, "es", "en"]))
    last_error: Exception | None = None
    for proxy in media._routes(url):
        base = {**media._options(proxy), "skip_download": True}
        try:
            with media.yt_dlp.YoutubeDL(base) as client:
                info = client.extract_info(url, download=False) or {}
            code, automatic = _pick_track(info, preferred)
            if not code:
                raise ValueError("no_subtitles")
            with tempfile.TemporaryDirectory(prefix="subs-") as folder:
                options = {**base, "writesubtitles": not automatic, "writeautomaticsub": automatic, "subtitleslangs": [code],
                           "subtitlesformat": "vtt", "outtmpl": str(Path(folder) / "subs.%(ext)s")}
                with media.yt_dlp.YoutubeDL(options) as client:
                    client.process_ie_result(info, download=True)
                files = list(Path(folder).glob("subs*.vtt"))
                if not files:
                    raise ValueError("no_subtitles")
                return str(info.get("title") or ""), _vtt_text(files[0].read_text(errors="ignore"))
        except ValueError:
            raise
        except Exception as exc:
            last_error = exc
    raise ValueError(media.error_code(last_error) if last_error else "no_subtitles")
