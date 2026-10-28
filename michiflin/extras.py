"""Quota-free utilities: translation, unit conversion, local time and video subtitles as text."""
from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import names
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


def translate(text: str, target: str, source: str = "auto") -> tuple[str, str]:
    """Return (translation, detected source language); empty translation when every service fails."""
    try:
        response = requests.get("https://translate.googleapis.com/translate_a/single",
                                params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text}, headers=UA, timeout=20)
        response.raise_for_status()
        data = response.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        if translated:
            return translated, str(data[2] or source)
    except (requests.RequestException, ValueError, IndexError, TypeError):
        pass
    try:
        pair = f"{'autodetect' if source == 'auto' else source}|{target}"
        response = requests.get("https://api.mymemory.translated.net/get", params={"q": text[:500], "langpair": pair}, timeout=20)
        response.raise_for_status()
        data = response.json()
        translated = str((data.get("responseData") or {}).get("translatedText") or "")
        if translated and data.get("responseStatus") == 200:
            return translated, source
    except (requests.RequestException, ValueError):
        pass
    return "", source


def translate_text(argument: str, locale: str) -> str:
    """'[language] text': the target defaults to Spanish, or English when the text is already Spanish."""
    words = argument.strip().split(maxsplit=1)
    if not words:
        return tr(locale, f"Uso: {names.cmd(locale, 'translate')} [idioma] texto · ej. {names.cmd(locale, 'translate')} inglés buenos días",
                  f"Usage: {names.cmd(locale, 'translate')} [language] text · e.g. {names.cmd(locale, 'translate')} spanish good morning")
    target = language_code(words[0]) if len(words) == 2 else ""
    text = words[1] if target else argument.strip()
    translated, detected = translate(text[:4500], target or "es")
    if translated and not target and detected.startswith("es"):
        target = "en"
        translated, detected = translate(text[:4500], "en")
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
