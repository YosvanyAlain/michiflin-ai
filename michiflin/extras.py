"""Quota-free utilities: translation, unit conversion, local time and video subtitles as text."""
from __future__ import annotations

import math
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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


# ISO-639-3 codes used by Apertium APy for the languages in the app.
_APERTIUM_LANGUAGES = {
    "es": "spa", "en": "eng", "fr": "fra", "pt": "por", "it": "ita", "de": "deu", "ru": "rus",
    "zh-CN": "zho", "ja": "jpn", "ko": "kor", "ar": "ara", "nl": "nld", "pl": "pol", "tr": "tur",
    "uk": "ukr", "hi": "hin", "sv": "swe", "el": "ell", "he": "heb", "ca": "cat", "ro": "ron",
    "vi": "vie", "id": "ind",
}
_APERTIUM_BY_CODE = {value: key for key, value in _APERTIUM_LANGUAGES.items()}
_TRANSLATION_BACKOFF: dict[str, float] = {}
_TRANSLATION_LOCK = threading.Lock()


class _TranslationFailure(Exception):
    """Non-sensitive internal reason; never includes input or provider response."""


def _translation_ready(provider: str) -> bool:
    with _TRANSLATION_LOCK:
        return time.monotonic() >= _TRANSLATION_BACKOFF.get(provider, 0.0)


def _translation_status(response: requests.Response, provider: str) -> None:
    if response.status_code == 429:
        # Honour Retry-After, or wait five minutes when the provider omits it.
        value = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
        delay = 300.0
        if value.isdigit():
            try:
                delay = max(1.0, float(value))
            except (ValueError, OverflowError):
                pass
        elif value:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = max(1.0, when.timestamp() - time.time())
            except (ValueError, OverflowError, TypeError):
                pass
        if not math.isfinite(delay):
            delay = 300.0
        with _TRANSLATION_LOCK:
            _TRANSLATION_BACKOFF[provider] = max(_TRANSLATION_BACKOFF.get(provider, 0.0), time.monotonic() + delay)
        raise _TranslationFailure("rate_limited")
    response.raise_for_status()


def _translation_timeout(deadline: float, maximum: int) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _TranslationFailure("unavailable")
    return max(0.1, min(float(maximum), left))


def _translation_https_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _TranslationFailure("unavailable")
    return value.rstrip("/")


def _google_translation(text: str, target: str, source: str, deadline: float) -> dict[str, str]:
    if not _translation_ready("google"):
        raise _TranslationFailure("rate_limited")
    translated, detected = [], source
    for segment, separator in _translation_chunks(text, 3500):
        state_check_translation()
        response = requests.get("https://translate.googleapis.com/translate_a/single",
                                params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": segment},
                                headers=UA, timeout=_translation_timeout(deadline, 12))
        _translation_status(response, "google")
        data = response.json()
        part = "".join(str(item[0]) for item in data[0] if item and item[0])
        if not part:
            raise _TranslationFailure("unavailable")
        if source == "auto" and len(data) > 2 and data[2] and detected == "auto":
            detected = str(data[2])
        translated.append(part + separator)
    return {"text": "".join(translated), "detected": detected, "provider": "Google Translate"}


def _libre_translation(text: str, target: str, source: str, deadline: float) -> dict[str, str]:
    endpoint = _translation_https_endpoint(config.LIBRETRANSLATE_URL.strip())
    if not _translation_ready("libre"):
        raise _TranslationFailure("rate_limited")
    translated, detected = [], source
    for segment, separator in _translation_chunks(text, 3500):
        state_check_translation()
        body = {"q": segment, "source": source, "target": target, "format": "text"}
        if config.LIBRETRANSLATE_API_KEY:
            body["api_key"] = config.LIBRETRANSLATE_API_KEY
        response = requests.post(endpoint, json=body, timeout=_translation_timeout(deadline, 18), allow_redirects=False)
        _translation_status(response, "libre")
        if response.status_code != 200:
            raise _TranslationFailure("unavailable")
        data = response.json()
        part = str(data.get("translatedText") or "")
        if not part:
            raise _TranslationFailure("unavailable")
        if source == "auto" and detected == "auto":
            detected = str((data.get("detectedLanguage") or {}).get("language") or "auto")
        translated.append(part + separator)
    return {"text": "".join(translated), "detected": detected, "provider": "LibreTranslate"}


def _apertium_translation(text: str, target: str, source: str, deadline: float) -> dict[str, str]:
    if not config.APERTIUM_ENABLED:
        raise _TranslationFailure("unavailable")
    endpoint = _translation_https_endpoint(config.APERTIUM_URL.strip())
    if not _translation_ready("apertium"):
        raise _TranslationFailure("rate_limited")
    detected = source
    if source == "auto":
        # Detection is fallible, especially for one-word inputs. Never silently
        # choose a low-confidence language; ask the person to select it instead.
        state_check_translation()
        sample = _translation_chunks(text, 1000)[0][0]
        response = requests.post(endpoint + "/identifyLang", data={"q": sample}, headers=UA,
                                 timeout=_translation_timeout(deadline, 12), allow_redirects=False)
        _translation_status(response, "apertium")
        if response.status_code != 200:
            raise _TranslationFailure("unavailable")
        scores = response.json()
        if not isinstance(scores, dict):
            raise _TranslationFailure("detection_uncertain")
        choices = [(str(code), float(score)) for code, score in scores.items()
                   if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)]
        best, confidence = max(choices, key=lambda item: item[1]) if choices else ("", 0.0)
        detected = _APERTIUM_BY_CODE.get(best, "")
        if not detected or confidence < 0.80:
            raise _TranslationFailure("detection_uncertain")
    if detected == target:
        return {"text": text, "detected": detected, "provider": "Apertium (beta)"}
    pair = _APERTIUM_LANGUAGES[detected] + "|" + _APERTIUM_LANGUAGES[target]
    translated = []
    for segment, separator in _translation_chunks(text, 3500):
        state_check_translation()
        # POST keeps the text out of the URL; the provider still receives it.
        response = requests.post(endpoint + "/translate", data={"q": segment, "langpair": pair, "format": "txt"},
                                 headers=UA, timeout=_translation_timeout(deadline, 12), allow_redirects=False)
        if response.status_code == 400:
            raise _TranslationFailure("pair_unavailable")
        _translation_status(response, "apertium")
        if response.status_code != 200:
            raise _TranslationFailure("unavailable")
        data = response.json()
        if not isinstance(data, dict) or data.get("responseStatus") != 200 or not isinstance(data.get("responseData"), dict):
            raise _TranslationFailure("unavailable")
        part = data["responseData"].get("translatedText")
        if not isinstance(part, str) or not part:
            raise _TranslationFailure("unavailable")
        # Preserve Apertium's * markers for words it does not recognize.
        translated.append(part + separator)
    return {"text": "".join(translated), "detected": detected, "provider": "Apertium (beta)"}


def translate_detailed(text: str, target: str, source: str = "auto") -> dict[str, str]:
    """Translate all-or-nothing without configured AI-model quota.

    Providers receive the text and can rate-limit it. Apertium is an independent,
    rule-based beta fallback, not a way around another provider's rate limit.
    Never silently guess a low-confidence language or return a partial result.
    """
    if not text or len(text) > 4500 or target not in LANGUAGES or (source != "auto" and source not in LANGUAGES):
        return {"text": "", "detected": source, "provider": "", "error": "invalid"}
    if source == target:
        return {"text": text, "detected": source, "provider": "identity"}
    deadline = time.monotonic() + 70
    reason = "unavailable"
    for translate_provider in (_google_translation, _libre_translation, _apertium_translation):
        try:
            return translate_provider(text, target, source, deadline)
        except _TranslationFailure as exc:
            # Prefer a specific actionable reason from the last independent provider.
            if str(exc) in {"detection_uncertain", "pair_unavailable", "rate_limited"}:
                reason = str(exc)
        except (requests.RequestException, ValueError, IndexError, TypeError, KeyError):
            continue
    return {"text": "", "detected": source, "provider": "", "error": reason}


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
