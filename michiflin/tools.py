"""Quota-free information tools: search, news, weather, rates, encyclopedia, calculator, GitHub."""
from __future__ import annotations

import ast
import hashlib
import math
import re
import threading
import time
import unicodedata
from defusedxml import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import requests

from . import config, links, rate_store, state
from .util import compact, local_time, tr, unlinked, host_of

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    DDGS = None  # type: ignore[assignment]

UA = {"User-Agent": "MichiflinAI/5 (private Delta Chat assistant)"}
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, seconds: int, producer: Callable[[], Any]) -> Any:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < seconds:
        return hit[1]
    value = producer()
    if value:
        _cache[key] = (time.monotonic(), value)
    return value


# ---------------------------------------------------------------------------
# Web search and news.
# ---------------------------------------------------------------------------
@dataclass
class Result:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    date: str = ""


ENCYCLOPEDIAS = ("wikipedia.org", "grokipedia.com")


def _words(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    return {word for word in re.findall(r"\w+", "".join(c for c in folded if not unicodedata.combining(c))) if len(word) > 2}


def _relevant(url: str, title: str, query: str) -> bool:
    """Encyclopedia shortcuts are fuzzy matches; keep them only when the title names the query."""
    host = host_of(url).lower()
    if not any(host == name or host.endswith("." + name) for name in ENCYCLOPEDIAS):
        return True
    return _words(query) <= _words(title)


def _ddgs_text(query: str, locale: str, limit: int) -> list[Result]:
    """Keyless web search; single backends fail at random, so a few backend and region pairs are tried within a time budget."""
    regions = ["us-en", "wt-wt"] if locale == "en" else ["xl-es", "es-es"]
    started = time.time()
    for region, backend in ((regions[0], "auto"), (regions[0], "bing"), (regions[1], "auto"), (regions[1], "bing"), (regions[0], "duckduckgo")):
        if time.time() - started > 25:
            break
        try:
            items = DDGS(timeout=10).text(query, region=region, safesearch="moderate", max_results=limit + 2, backend=backend)
        except Exception:
            continue
        found = []
        for item in items or []:
            url = str(item.get("href") or item.get("url") or "").strip()
            if url.startswith("http") and _relevant(url, str(item.get("title") or ""), query):
                found.append(Result(compact(item.get("title"), 140), url, compact(item.get("body"), 260)))
        if found:
            return found[:limit]
    return []


def search(query: str, locale: str, limit: int = 6, engine: str = "standard") -> list[Result]:
    """Web results. The standard engine is keyless and has no quota; the advanced one spends You.com calls and falls back to standard."""
    if DDGS is None or not query.strip():
        return []
    if engine == "advanced":
        found = _cached(f"y:{limit}:{query.lower()}", 1800, lambda: _you_search(query, limit))
        if found:
            return found
    return _cached(f"s:{locale}:{limit}:{query.lower()}", 600, lambda: _ddgs_text(query, locale, limit))


def _views(count: Any, locale: str) -> str:
    try:
        value = int(count or 0)
    except (TypeError, ValueError):
        return ""
    if not value:
        return ""
    for size, suffix in ((10**9, " mil M" if locale != "en" else "B"), (10**6, " M"), (10**3, " K")):
        if value >= size:
            number = f"{value / size:.1f}".rstrip("0").rstrip(".")
            return f"{number}{suffix} " + tr(locale, "vistas", "views")
    return f"{value} " + tr(locale, "vistas", "views")


def _clock(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return str(seconds or "")
    if not total:
        return ""
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def _youtube_search(query: str, limit: int, locale: str) -> list[Result]:
    from . import media

    if not media.available():
        return []
    for proxy in media._routes("https://www.youtube.com/"):
        options = {**media._options(proxy), "extract_flat": "in_playlist", "skip_download": True, "noplaylist": False,
                   "socket_timeout": 20, "retries": 1}
        try:
            with media.yt_dlp.YoutubeDL(options) as client:
                info = client.extract_info(f"ytsearch{limit}:{query}", download=False) or {}
        except Exception:
            continue
        found = []
        for item in info.get("entries") or []:
            url = str(item.get("url") or "")
            if not url.startswith("http") and item.get("id"):
                url = f"https://www.youtube.com/watch?v={item['id']}"
            if "/shorts/" in url or not url.startswith("http"):
                continue
            meta = " · ".join(bit for bit in (compact(item.get("channel") or item.get("uploader"), 50), _clock(item.get("duration")),
                                              _views(item.get("view_count"), locale)) if bit)
            found.append(Result(compact(item.get("title"), 140), url, meta, "YouTube"))
        if found:
            return found
    return []


def _ddg_videos(query: str, limit: int, locale: str) -> list[Result]:
    if DDGS is None:
        return []
    region = "us-en" if locale == "en" else "xl-es"
    found = []
    for item in DDGS().videos(query, region=region, safesearch="moderate", max_results=limit + 5) or []:
        url = str(item.get("content") or "").strip()
        if not url.startswith("http"):
            continue
        meta = " · ".join(bit for bit in (compact(item.get("uploader"), 50), compact(item.get("duration"), 10),
                                          _views((item.get("statistics") or {}).get("viewCount"), locale),
                                          compact(item.get("publisher"), 20)) if bit)
        found.append(Result(compact(item.get("title"), 140), url, meta, compact(item.get("publisher"), 20),
                            str(item.get("published") or "")[:10]))
    found.sort(key=lambda result: result.source != "YouTube")
    return found[:limit]


def video_search(query: str, locale: str, limit: int = 15) -> list[Result]:
    """Videos for a query: YouTube search via yt-dlp, DuckDuckGo videos as fallback."""
    if not query.strip():
        return []

    def run() -> list[Result]:
        found = _youtube_search(query, limit, locale)
        if found:
            return found
        try:
            return _ddg_videos(query, limit, locale)
        except Exception:
            return []

    return _cached(f"vs:{locale}:{limit}:{query.lower()}", 900, run)


# Browsing beyond the first screen is paginated, bounded and isolated by sender.
# A snapshot keeps earlier pages stable when YouTube's ranking changes between
# fetches. yt-dlp may still return fewer results or fail under site restrictions.
VIDEO_PAGE_SIZE = 15
VIDEO_MAX_PAGES = 12
VIDEO_BATCH = 45
_VIDEO_CACHE_SECONDS = 300
_video_cache: dict[str, tuple[float, list[Result], int, bool]] = {}
_video_lock = threading.RLock()


def _youtube_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return ""
        if host == "youtu.be":
            value = parsed.path.strip("/").split("/")[0]
        elif host == "youtube.com" or host.endswith(".youtube.com"):
            value = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            return ""
        return value if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", value) else ""
    except ValueError:
        return ""


def youtube_page(query: str, locale: str, page: int, owner: str) -> dict[str, Any]:
    """Stable incremental pages, never claim that the provider has infinite results."""
    if type(page) is not int or not 1 <= page <= VIDEO_MAX_PAGES:
        raise ValueError("page")
    query = query.strip()[:300]
    if not query or not owner:
        raise ValueError("query")
    cache_key = hashlib.sha256((owner.casefold() + "\0" + locale + "\0" + query.casefold()).encode()).hexdigest()
    now = time.monotonic()
    with _video_lock:
        for key, item in list(_video_cache.items()):
            if now - item[0] > _VIDEO_CACHE_SECONDS:
                _video_cache.pop(key, None)
        if len(_video_cache) > 128:
            _video_cache.pop(min(_video_cache, key=lambda key: _video_cache[key][0]), None)
        hit = _video_cache.get(cache_key)
    saved = list(hit[1]) if hit else []
    requested = hit[2] if hit else 0
    exhausted = hit[3] if hit else False
    end = page * VIDEO_PAGE_SIZE
    target = min(VIDEO_MAX_PAGES * VIDEO_PAGE_SIZE + 1,
                 max(VIDEO_BATCH + 1, ((end // VIDEO_BATCH) + 1) * VIDEO_BATCH + 1))
    if len(saved) <= end and not exhausted and target > requested:
        try:
            found = _youtube_search(query, target, locale)
            if not found:
                found = _ddg_videos(query, target, locale)
        except Exception:
            found = []
        seen = {_youtube_id(item.url) for item in saved}
        for item in found:
            video_id = _youtube_id(item.url)
            if video_id and video_id not in seen:
                seen.add(video_id)
                saved.append(item)
        # A provider may stop early, but another request should not repeat the
        # same expensive search in this short session.
        exhausted = len(found) < target
        requested = target
        with _video_lock:
            _video_cache[cache_key] = (time.monotonic(), saved, requested, exhausted)
    start = (page - 1) * VIDEO_PAGE_SIZE
    return {"results": saved[start:end], "more": len(saved) > end and page < VIDEO_MAX_PAGES,
            "limited": len(saved) > end and page == VIDEO_MAX_PAGES}


def you_left() -> int:
    """You.com calls left today for the advanced search engine."""
    if not config.YDC_API_KEY or state.cooldown_until("you:search"):
        return 0
    return max(0, config.YDC_DAILY - state.usage_today("you:search"))


def _you_search(query: str, limit: int) -> list[Result]:
    """You.com Web Search within its free daily calls; empty when unavailable."""
    key = "you:search"
    if not config.YDC_API_KEY or state.usage_today(key) >= config.YDC_DAILY or state.cooldown_until(key):
        return []
    try:
        response = requests.get("https://ydc-index.io/v1/search", params={"query": query, "count": min(20, limit + 2), "safesearch": "moderate"},
                                headers={"X-API-Key": config.YDC_API_KEY}, timeout=20)
    except requests.RequestException:
        state.set_cooldown(key, 300)
        return []
    state.record_usage(key, response.ok)
    if not response.ok:
        state.set_cooldown(key, 6 * 3600 if response.status_code in {401, 402, 403, 429} else 600)
        return []
    found = []
    for item in ((response.json().get("results") or {}).get("web") or []):
        url = str(item.get("url") or "").strip()
        snippets = item.get("snippets") or []
        snippet = item.get("description") or (snippets[0] if snippets else "")
        if url.startswith("http"):
            found.append(Result(compact(item.get("title"), 140), url, compact(snippet, 260), date=str(item.get("page_age") or "")[:10]))
    return found[:limit]


def news_results(query: str, locale: str, limit: int = 7) -> list[Result]:
    region = "us-en" if locale == "en" else "xl-es"
    found: list[Result] = []
    if DDGS is not None:
        try:
            for item in DDGS().news(query, region=region, safesearch="moderate", max_results=limit) or []:
                url = str(item.get("url") or "").strip()
                if url.startswith("http"):
                    found.append(Result(compact(item.get("title"), 200), url, compact(item.get("body"), 200),
                                        compact(item.get("source"), 60), str(item.get("date") or "")[:10]))
        except Exception:
            found = []
    if found:
        return found[:limit]
    language = "hl=en-US&gl=US&ceid=US:en" if locale == "en" else "hl=es-419&gl=US&ceid=US:es-419"
    response = requests.get(f"https://news.google.com/rss/search?q={quote_plus(query)}&{language}", timeout=25, headers=UA)
    response.raise_for_status()
    for item in ET.fromstring(response.content).findall("./channel/item")[:limit]:
        source = item.find("source")
        found.append(Result(compact(item.findtext("title"), 200), (item.findtext("link") or "").strip(), "",
                            source.text.strip() if source is not None and source.text else ""))
    return found


def search_text(query: str, locale: str) -> str:
    if not query.strip():
        return tr(locale, "Uso: /buscar <consulta>", "Usage: /search <query>")
    try:
        results = search(query, locale)
    except Exception as exc:
        return tr(locale, f"⚠️ La búsqueda falló ({exc.__class__.__name__}).", f"⚠️ Search failed ({exc.__class__.__name__}).")
    if not results:
        return tr(locale, "🔎 Sin resultados.", "🔎 No results.")
    lines = [f"🔎 {compact(query, 100)}", ""]
    lines += [links.entry(index, item.title, item.url, item.snippet) for index, item in enumerate(results, 1)]
    lines += ["", tr(locale, "Toca un comando para abrir la página.", "Tap a command to open the page.")]
    return "\n".join(lines)


def news_text(query: str, locale: str) -> str:
    query = query.strip() or tr(locale, "Cuba", "world news")
    try:
        results = news_results(query, locale)
    except Exception as exc:
        return tr(locale, f"⚠️ No pude consultar noticias ({exc.__class__.__name__}).", f"⚠️ News lookup failed ({exc.__class__.__name__}).")
    if not results:
        return tr(locale, f"📰 Sin titulares para “{query}”.", f"📰 No headlines for “{query}”.")
    lines = [tr(locale, f"📰 Noticias: {query}", f"📰 News: {query}"), ""]
    for index, item in enumerate(results, 1):
        meta = " · ".join(filter(None, [item.source, item.date]))
        lines.append(f"{index}. {item.title}")
        lines.append(f"   {meta + ' · ' if meta else ''}{links.command('v', item.url)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Encyclopedia answers (Wikipedia REST, no AI quota).
# ---------------------------------------------------------------------------
def wiki_summary(topic: str, locale: str) -> tuple[str, str, str] | None:
    """Return (title, extract, url) for a topic, or None."""
    language = "en" if locale == "en" else "es"
    topic = compact(topic, 120)
    if not topic:
        return None

    def run() -> tuple[str, str, str] | None:
        found = requests.get(
            f"https://{language}.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"},
            headers=UA, timeout=15,
        )
        found.raise_for_status()
        hits = found.json().get("query", {}).get("search", [])
        if not hits:
            return None
        title = hits[0]["title"]
        response = requests.get(f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}", headers=UA, timeout=15)
        if not response.ok:
            return None
        data = response.json()
        extract = compact(data.get("extract"), 1400)
        if len(extract) >= 1400 and ". " in extract:
            extract = extract.rsplit(". ", 1)[0] + "."
        if not extract or data.get("type") == "disambiguation":
            return None
        url = (data.get("content_urls") or {}).get("desktop", {}).get("page") or f"https://{language}.wikipedia.org/wiki/{quote(title)}"
        return data.get("title", title), extract, url

    try:
        return _cached(f"w:{language}:{topic.lower()}", 3600, run)
    except (requests.RequestException, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, keyless).
# ---------------------------------------------------------------------------
WEATHER = {
    0: ("despejado", "clear"), 1: ("mayormente despejado", "mostly clear"), 2: ("parcialmente nublado", "partly cloudy"),
    3: ("nublado", "overcast"), 45: ("niebla", "fog"), 48: ("niebla con escarcha", "rime fog"),
    56: ("llovizna helada ligera", "light freezing drizzle"), 57: ("llovizna helada intensa", "heavy freezing drizzle"),
    66: ("lluvia helada ligera", "light freezing rain"), 67: ("lluvia helada intensa", "heavy freezing rain"),
    77: ("granos de nieve", "snow grains"), 85: ("chubascos de nieve", "snow showers"), 86: ("chubascos de nieve fuertes", "heavy snow showers"),
    51: ("llovizna ligera", "light drizzle"), 53: ("llovizna", "drizzle"), 55: ("llovizna intensa", "heavy drizzle"),
    61: ("lluvia ligera", "light rain"), 63: ("lluvia", "rain"), 65: ("lluvia intensa", "heavy rain"),
    71: ("nieve ligera", "light snow"), 73: ("nieve", "snow"), 75: ("nieve intensa", "heavy snow"),
    80: ("chubascos ligeros", "light showers"), 81: ("chubascos", "showers"), 82: ("chubascos fuertes", "heavy showers"),
    95: ("tormenta", "thunderstorm"), 96: ("tormenta con granizo", "thunderstorm with hail"), 99: ("tormenta fuerte con granizo", "severe thunderstorm"),
}


class WeatherError(Exception):
    pass


def _place_label(place: dict[str, Any]) -> str:
    return ", ".join(str(item) for item in (place.get("name"), place.get("admin1"), place.get("country")) if item)


def weather_data(query: str, locale: str, place_id: int = 0) -> dict[str, Any]:
    """Geocode explicitly, allowing the user to resolve places with the same name."""
    query = query.strip()[:120]
    if not query:
        raise WeatherError("empty")
    parts = [part.strip() for part in query.split(",") if part.strip()]
    name = parts[0] if parts else query
    try:
        geocode = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": name, "count": 10, "language": locale, "format": "json"}, timeout=15)
        geocode.raise_for_status()
        places = geocode.json().get("results") or []
        if len(parts) > 1:
            needle = " ".join(parts[1:]).casefold()
            matched = [p for p in places if needle in _place_label(p).casefold()]
            if matched:
                places = matched
        places = [p for p in places if isinstance(p, dict) and p.get("id") and p.get("latitude") is not None and p.get("longitude") is not None]
        if not places:
            raise WeatherError("not_found")
        place = next((p for p in places if p["id"] == place_id), None) if place_id else places[0]
        if place is None:
            raise WeatherError("place_changed")
        forecast = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 5, "timezone": "auto",
        }, timeout=15)
        forecast.raise_for_status()
        body = forecast.json()
        current = body.get("current") or {}
        daily = body.get("daily") or {}
        if "temperature_2m" not in current or "weather_code" not in current:
            raise WeatherError("empty")
        index = 1 if locale == "en" else 0
        days = []
        for day, code, high, low, rain in zip(daily.get("time", []), daily.get("weather_code", []),
                daily.get("temperature_2m_max", []), daily.get("temperature_2m_min", []), daily.get("precipitation_probability_max", [])):
            days.append({"date": day, "code": code, "description": WEATHER.get(int(code), ("?", "?"))[index],
                         "high": high, "low": low, "rain": rain})
        return {"query": query, "place": {"id": place["id"], "label": _place_label(place)},
                "candidates": [{"id": p["id"], "label": _place_label(p)} for p in places[:10]],
                "timezone": body.get("timezone") or place.get("timezone") or "", "as_of": current.get("time") or "",
                "current": {"temp": current.get("temperature_2m"), "feels": current.get("apparent_temperature"),
                            "humidity": current.get("relative_humidity_2m"), "wind": current.get("wind_speed_10m"),
                            "precipitation": current.get("precipitation"), "code": current.get("weather_code"),
                            "is_day": current.get("is_day", 1),
                            "description": WEATHER.get(int(current["weather_code"]), ("?", "?"))[index]},
                "days": days, "source": "Open-Meteo"}
    except WeatherError:
        raise
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
        raise WeatherError("unavailable") from exc


def weather_text(query: str, locale: str) -> str:
    if not query.strip():
        return tr(locale, "Uso: /clima <ciudad>", "Usage: /weather <city>")
    try:
        data = weather_data(query, locale)
    except WeatherError as exc:
        if str(exc) == "not_found":
            return tr(locale, f"🌦️ No encontré “{query}”.", f"🌦️ “{query}” was not found.")
        return tr(locale, "⚠️ No pude consultar el clima ahora.", "⚠️ Weather lookup failed right now.")
    now = data["current"]
    lines = [f"🌦️ {data['place']['label']}",
             f"{now['description'].capitalize()} · {now['temp']} °C ({tr(locale, 'sensación', 'feels')} {now['feels']} °C)",
             f"{tr(locale, 'Humedad', 'Humidity')} {now['humidity']}% · {tr(locale, 'Viento', 'Wind')} {now['wind']} km/h · {now['precipitation']} mm", ""]
    for day in data["days"]:
        name = datetime.fromisoformat(day["date"]).strftime("%d/%m")
        lines.append(f"{name}: {day['description']} · {day['low']}–{day['high']} °C · 💧{day['rain']}%")
    lines.append("\nOpen-Meteo · " + data["as_of"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# elTOQUE informal exchange rates through the authorized API only.
# ---------------------------------------------------------------------------
ELTOQUE_ENDPOINT = "https://tasas.eltoque.com/v1/trmi"
# The provider documents per-minute and per-second limits; serialize all
# requests in this process and stay below the lower published steady rate.
ELTOQUE_MIN_SECONDS = 1.1
ELTOQUE_FALLBACK_COOLDOWN = 60
ELTOQUE_HISTORY_BUDGET = 90.0  # return actual partial points before the bridge timeout
_eltoque_request_lock = threading.Lock()
_eltoque_current_lock = threading.Lock()
_eltoque_history_lock = threading.Lock()
_eltoque_next_request = 0.0
_eltoque_blocked_until = 0.0


class RateError(Exception):
    def __init__(self, reason: str, retry_after: int | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


def rate_unit(code: str) -> str:
    """Denomination of the unchanged API value, not a whole-coin guess.

    elTOQUE's published crypto table explicitly marks BTC and USDT (TRC20)
    as CUP per USD represented in the asset, and TRX as CUP per token. ECU is
    the API's legacy Euro code (the API does not document this alias); its
    value has been cross-checked with elTOQUE's Euro table. Unknown future
    variants remain unknown, rather than inheriting a suffix's denomination.
    """
    code = str(code).upper()
    if code in {"BTC", "USDT", "USDT_TRC20"}:
        return "usd_crypto"
    if code in {"USD", "EUR", "ECU", "MLC", "TRX", "CAD", "MXN", "CUP"}:
        return "unit"
    return "unknown"


def rate_display_code(code: str) -> str:
    """Display human-friendly names without changing source keys or values."""
    return {"ECU": "EUR", "USDT_TRC20": "USDT (TRC20)"}.get(code, code)


def rate_units(rates: dict[str, float]) -> dict[str, str]:
    return {code: rate_unit(code) for code in rates}


def _parse_eltoque_rates(payload: Any) -> dict[str, float]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tasas"), dict):
        raise RateError("invalid")
    rates = {}
    for code, value in payload["tasas"].items():
        code = str(code).upper()
        if not re.fullmatch(r"[A-Z0-9_-]{2,32}", code) or isinstance(value, bool):
            continue
        try:
            number = float(str(value).replace(",", "."))
        except (ValueError, TypeError):
            continue
        if math.isfinite(number) and number > 0:
            rates[code] = number
    if not rates:
        raise RateError("empty")
    return rates


def _retry_after(headers: Any) -> int:
    """Honor a valid Retry-After. Invalid/negative headers get a safe pause.

    HTTP dates are compared against the server's Date when present, which
    also avoids depending on synchronized local/server clocks.
    """
    raw = str((headers or {}).get("Retry-After") or "").strip()
    try:
        number = float(raw)
        if math.isfinite(number) and number > 0:
            return max(1, math.ceil(number))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        date_header = (headers or {}).get("Date")
        origin = parsedate_to_datetime(date_header) if date_header else datetime.now(timezone.utc)
        delay = (target - origin).total_seconds()
        if math.isfinite(delay) and delay > 0:
            return max(1, math.ceil(delay))
    except (TypeError, ValueError, OverflowError):
        pass
    return ELTOQUE_FALLBACK_COOLDOWN


def _eltoque_request(params: dict[str, str] | None = None, *, deadline: float | None = None) -> dict[str, float]:
    """Make one bounded authenticated request without redirects or fallback."""
    global _eltoque_next_request, _eltoque_blocked_until
    state.check_work_allowed()
    remaining = deadline - time.monotonic() if deadline is not None else None
    if remaining is not None:
        if remaining <= 0 or not _eltoque_request_lock.acquire(timeout=remaining):
            raise RateError("timeout")
    else:
        _eltoque_request_lock.acquire()
    try:
        state.check_work_allowed()
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            raise RateError("timeout")
        if now < _eltoque_blocked_until:
            raise RateError("rate_limit", max(1, math.ceil(_eltoque_blocked_until - now)))
        delay = max(0.0, _eltoque_next_request - now)
        if deadline is not None and now + delay >= deadline:
            raise RateError("timeout")
        if delay:
            time.sleep(delay)
            state.check_work_allowed()
        if deadline is not None and time.monotonic() >= deadline:
            raise RateError("timeout")
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise RateError("timeout")
        _eltoque_next_request = time.monotonic() + ELTOQUE_MIN_SECONDS
        # requests' connect and read timeouts are separate, so divide the
        # remaining budget between them instead of promising a hard 25 s total.
        timeout = 25.0 if remaining is None else max(0.01, min(10.0, remaining / 2))
        try:
            response = requests.get(ELTOQUE_ENDPOINT, timeout=timeout, params=params,
                headers={"Authorization": f"Bearer {config.ELTOQUE_API_TOKEN}", "Accept": "application/json"},
                allow_redirects=False)
        except requests.RequestException as exc:
            raise RateError("connection") from exc
        if response.status_code in {401, 403}:
            raise RateError("authorization")
        if response.status_code == 422:
            # The live service returns {"msg": ...} for malformed JWTs, but
            # 422 can also mean invalid input; do not label every 422 as auth.
            try:
                body = response.json()
            except (ValueError, TypeError):
                body = None
            message = str(body.get("msg") or "").lower() if isinstance(body, dict) else ""
            auth_error = any(part in message for part in ("jwt", "token", "segment", "signature", "invalid header", "invalid claim", "expired", "authorization"))
            raise RateError("authorization" if auth_error else "http")
        if response.status_code == 429:
            wait = _retry_after(getattr(response, "headers", {}))
            _eltoque_blocked_until = time.monotonic() + wait
            raise RateError("rate_limit", wait)
        # In particular, never follow a redirect with the bearer token.
        if response.status_code != 200:
            raise RateError("http")
        try:
            return _parse_eltoque_rates(response.json())
        except (ValueError, TypeError, AttributeError) as exc:
            raise RateError("invalid") from exc
    finally:
        _eltoque_request_lock.release()


def _rates() -> dict[str, float]:
    if not config.ELTOQUE_API_TOKEN:
        raise RateError("authorization")
    with _eltoque_current_lock:
        return _cached("rates", config.ELTOQUE_REFRESH_SECONDS, _eltoque_request)


def current_rates() -> dict[str, float]:
    # A rejected/rate-limited official call must not silently use another
    # endpoint to bypass the provider's control or mislabel the source.
    return _rates()


def rates_observed_at() -> float:
    """Wall-clock time of the last successful fetch, not the current refresh."""
    hit = _cache.get("rates")
    return time.time() - max(0.0, time.monotonic() - hit[0]) if hit else time.time()


def saved_rates_snapshot() -> dict[str, Any] | None:
    """Read shared data without contacting the API (for the home banner)."""
    previous = rate_store.latest()
    if not previous:
        return None
    health = rate_store.status()
    stale = bool(health.get("error")) or time.time() - previous["at"] > 2 * config.ELTOQUE_REFRESH_SECONDS
    return {**previous, "units": rate_units(previous["rates"]), "stale": stale, "monitor": health,
            "persisted": True, "source": "elTOQUE API", "refresh_seconds": config.ELTOQUE_REFRESH_SECONDS,
            "derived_allowed": config.ELTOQUE_DERIVED_CALCULATIONS}


def rates_snapshot() -> dict[str, Any]:
    """Share one API snapshot; clearly mark the last saved quote on failure."""
    try:
        rates = current_rates()
    except RateError as exc:
        previous = rate_store.latest()
        if not previous:
            raise
        return {**previous, "stale": True, "error": str(exc), "retry_after": exc.retry_after,
                "persisted": True, "source": "elTOQUE API"}
    at = rates_observed_at()
    saved = rate_store.save_latest(rates, at)
    return {"rates": rates, "at": at, "stale": False, "error": "", "retry_after": None,
            "persisted": saved, "source": "elTOQUE API"}


def historical_rates(days: int = 7) -> dict[str, Any]:
    """Use saved real day windows and fetch only gaps, respecting cooldowns."""
    if type(days) is not int or days not in {7, 14, 30}:
        raise RateError("range")
    end = datetime.now(config.LOCAL_ZONE).date() - timedelta(days=1)
    key = f"rates_history:{end.isoformat()}:{days}"
    if not _eltoque_history_lock.acquire(blocking=False):
        raise RateError("busy")
    deadline = time.monotonic() + ELTOQUE_HISTORY_BUDGET
    try:
        state.check_work_allowed()
        hit = _cache.get(key)
        if config.ELTOQUE_API_TOKEN and hit and time.monotonic() - hit[0] < 3600:
            return hit[1]
        series: list[dict[str, Any]] = []
        new_days: dict[str, dict[str, Any]] = {}
        error, retry_after = ("" if config.ELTOQUE_API_TOKEN else "authorization"), None
        for age in range(days - 1, -1, -1):
            state.check_work_allowed()
            day = (end - timedelta(days=age)).isoformat()
            day_key = f"rates_day:{day}"
            hit = _cache.get(day_key)
            saved = rate_store.day(day)
            if hit and time.monotonic() - hit[0] < 3600:
                rates, observed = hit[1], (saved or {}).get("at", time.time())
            elif saved:
                rates, observed = saved["rates"], saved["at"]
                _cache[day_key] = (time.monotonic(), rates)
            elif error:
                continue  # still return later saved dates; do not bypass the cooldown
            else:
                try:
                    rates = _eltoque_request({"date_from": day + " 00:00:00", "date_to": day + " 23:59:59"}, deadline=deadline)
                except RateError as exc:
                    error, retry_after = str(exc), exc.retry_after
                    continue
                observed = time.time()
                new_days[day] = {"rates": rates, "at": observed}
                _cache[day_key] = (time.monotonic(), rates)
            series.append({"date": day, "rates": rates, "queried_at": observed})
        persisted = rate_store.save_days(new_days)
        if not series:
            raise RateError(error or "empty", retry_after)
        all_codes = {code for item in series for code in item["rates"]}
        result = {"history": series, "complete": len(series) == days, "error": error,
                  "retry_after": retry_after, "stale": bool(error), "persisted": persisted,
                  "units": {code: rate_unit(code) for code in sorted(all_codes)},
                  "source": "elTOQUE API", "days": days,
                  "derived_allowed": config.ELTOQUE_DERIVED_CALCULATIONS}
        if result["complete"] and not error:
            _cache[key] = (time.monotonic(), result)
        return result
    finally:
        _eltoque_history_lock.release()


def _number(value: float, locale: str) -> str:
    decimals = 6 if 0 < abs(value) < 0.01 else 2
    text = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if locale != "en" else text


def _rate_number(value: float, locale: str) -> str:
    """Format the received rate without forcing it to two decimals."""
    text = format(Decimal(str(value)), ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if locale != "en" else text


def rate_text(argument: str, locale: str) -> str:
    usage = tr(locale,
        "Uso: /tasa · /tasa EUR · /tasa 100 EUR · /tasa 5000 CUP EUR · /tasa 100 USD_BTC",
        "Usage: /rate · /rate EUR · /rate 100 EUR · /rate 5000 CUP EUR · /rate 100 USD_BTC")
    parts = argument.upper().replace(",", ".").split()
    try:
        snapshot = rates_snapshot()
        rates = snapshot["rates"]
    except RateError as exc:
        if str(exc) == "authorization":
            return tr(locale, "⚠️ La API de elTOQUE necesita una clave válida para esta aplicación.", "⚠️ The elTOQUE API requires a valid key for this application.")
        if str(exc) == "rate_limit":
            return tr(locale, "⏳ elTOQUE limitó las consultas; espera y vuelve a intentarlo.", "⏳ elTOQUE rate limit reached; wait before trying again.")
        return tr(locale, "⚠️ No pude consultar elTOQUE ahora.", "⚠️ elTOQUE is unavailable right now.")
    except (ValueError, requests.RequestException):
        return tr(locale, "⚠️ elTOQUE devolvió datos inválidos.", "⚠️ elTOQUE returned invalid data.")
    footer = tr(locale, f"elTOQUE · mercado informal · {local_time(snapshot['at'])}", f"elTOQUE · informal market · {local_time(snapshot['at'])}")
    if snapshot.get("stale"):
        footer += tr(locale, " · último dato guardado; la API no respondió ahora", " · last saved value; the API is unavailable now")

    def label(code: str) -> str:
        shown, kind = rate_display_code(code), rate_unit(code)
        if kind == "usd_crypto":
            return tr(locale, f"1 USD en {shown}", f"1 USD in {shown}")
        if kind == "unknown":
            return tr(locale, f"{shown} (unidad por confirmar)", f"{shown} (unit unconfirmed)")
        return f"1 {shown}"

    if not parts:
        preferred = ["USD", "EUR", "ECU", "MLC", "ZELLE", "USDT", "USDT_TRC20", "CAD", "MXN", "BTC", "TRX"]
        ordered = [code for code in preferred if code in rates] + sorted(code for code in rates if code not in preferred)
        return "\n".join([tr(locale, "💱 Tasas de referencia de elTOQUE", "💱 elTOQUE reference rates")]
                         + [f"{label(code)}: {_rate_number(rates[code], locale)} CUP" for code in ordered] + ["", footer])
    try:
        if len(parts) == 1:
            amount, source_input, target_input = 1.0, parts[0], "CUP"
        elif len(parts) == 2:
            amount, source_input, target_input = float(parts[0]), parts[1], "CUP"
        elif len(parts) == 3:
            amount, source_input, target_input = float(parts[0]), parts[1], parts[2]
        else:
            return usage
    except ValueError:
        return usage
    if not math.isfinite(amount) or amount < 0:
        return usage
    value = {"CUP": 1.0, **rates}
    explicit_crypto = {"USD_BTC": "BTC", "USD_USDT": "USDT", "USD_USDT_TRC20": "USDT_TRC20"}

    def resolve(code: str) -> str:
        if code in explicit_crypto:
            return explicit_crypto[code]
        return "ECU" if code == "EUR" and "EUR" not in value and "ECU" in value else code

    source, target = resolve(source_input), resolve(target_input)
    if source not in value or target not in value:
        missing = source_input if source not in value else target_input
        return tr(locale, f"⚠️ Sin tasa para {missing}.", f"⚠️ No rate for {missing}.")
    for raw, code in ((source_input, source), (target_input, target)):
        kind = rate_unit(code)
        if kind == "usd_crypto" and raw not in explicit_crypto:
            quote = f"{label(code)} ≈ {_rate_number(rates[code], locale)} CUP"
            if len(parts) == 1:
                return f"💱 {quote}\n\n{footer}"
            example = "USD_BTC" if code == "BTC" else ("USD_USDT_TRC20" if code == "USDT_TRC20" else "USD_USDT")
            return tr(locale, f"⚠️ {quote}. Es por dólar representado, no por una moneda completa. Para convertir dólares en ese activo usa {example}.",
                      f"⚠️ {quote}. This is per represented dollar, not per whole coin. To convert dollars in that asset use {example}.")
        if kind == "unknown":
            return tr(locale, f"⚠️ {code}: {_rate_number(rates[code], locale)}. La unidad de este código no está confirmada; no puedo convertirla.",
                      f"⚠️ {code}: {_rate_number(rates[code], locale)}. The unit for this code is unconfirmed; I cannot convert it.")
    if len(parts) == 1 or (len(parts) == 2 and amount == 1 and target == "CUP"):
        return f"💱 {label(source)} ≈ {_rate_number(value[source], locale)} CUP\n\n{footer}"
    if snapshot.get("stale"):
        return tr(locale, "La conversión está pausada porque la tasa guardada puede estar desactualizada.",
                  "Conversion is paused because the saved rate may be out of date.")
    if not config.ELTOQUE_DERIVED_CALCULATIONS:
        return tr(locale, "Las tasas se muestran sin alterar. Las conversiones están desactivadas en la configuración.",
                  "Rates are displayed unchanged. Conversions are disabled in the configuration.")
    converted = amount * value[source] / value[target]
    if not math.isfinite(converted):
        return tr(locale, "⚠️ La cantidad es demasiado grande para convertirla.", "⚠️ The amount is too large to convert.")

    def amount_label(code: str) -> str:
        shown = rate_display_code(code)
        return tr(locale, f"USD en {shown}", f"USD in {shown}") if rate_unit(code) == "usd_crypto" else shown

    note = tr(locale, "Conversión calculada por Michiflin; no es una cotización nueva de elTOQUE.",
              "Conversion calculated by Michiflin; it is not a new elTOQUE quote.")
    return f"💱 {_number(amount, locale)} {amount_label(source)} ≈ {_number(converted, locale)} {amount_label(target)}\n{note}\n\n{footer}"


# ---------------------------------------------------------------------------
# Calculator (safe AST evaluation).
# ---------------------------------------------------------------------------
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "raiz": math.sqrt, "log": math.log10, "ln": math.log, "sin": math.sin, "sen": math.sin,
    "cos": math.cos, "tan": math.tan, "abs": abs, "round": round, "redondear": round, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_OPERATORS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 64 or abs(left) > 10**6):
            raise ValueError
        value = _OPERATORS[type(node.op)](left, right)
        if isinstance(value, complex) or not math.isfinite(float(value)) or abs(value) > 10**18:
            raise ValueError
        return value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and len(node.args) <= 2 and not node.keywords:
        return _FUNCTIONS[node.func.id](*[_evaluate(arg) for arg in node.args])
    raise ValueError


QUESTION = re.compile(r"^[¿¡]?\s*(?:cu[aá]nto\s+(?:es|son|da)|calcula|calculate|what\s+is|what's|how\s+much\s+is)\s+(?:el\s+|la\s+)?", re.I)


def normalize_expression(text: str) -> str:
    value = text.strip().lower().rstrip("=?").strip()
    value = QUESTION.sub("", value)
    value = value.replace("×", "*").replace("÷", "/").replace("^", "**").replace("√", "sqrt")
    value = re.sub(r"(\d),(\d{3})\b", r"\1\2", value)
    value = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*(de|of)\s*(\d+(?:\.\d+)?)", r"(\1/100*\3)", value)
    value = re.sub(r"\bx\b", "*", value)
    return value


def calculate(expression: str) -> str | None:
    value = normalize_expression(expression)
    if not value or len(value) > 160 or not re.search(r"\d", value):
        return None
    try:
        result = _evaluate(ast.parse(value, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return format(result, ".12g") if isinstance(result, float) else str(result)


def calc_text(expression: str, locale: str) -> str:
    if not expression.strip():
        return tr(locale, "Uso: /calc (18+6)*3/2 · sqrt(2) · 15% de 300", "Usage: /calc (18+6)*3/2 · sqrt(2) · 15% of 300")
    result = calculate(expression)
    if result is None:
        return tr(locale, "⚠️ Expresión no válida.", "⚠️ Invalid expression.")
    shown = QUESTION.sub("", expression.strip()).rstrip("=? ")
    return f"🧮 {shown} = {result}"


def looks_like_math(text: str) -> bool:
    value = normalize_expression(text)
    return bool(re.fullmatch(r"[\d\s.+\-*/%()a-z,]+", value)) and bool(re.search(r"\d\s*[-+*/%]|\bsqrt|\braiz|\bde\b", value)) and calculate(text) is not None


# ---------------------------------------------------------------------------
# Technical references (official public APIs; no AI).
# ---------------------------------------------------------------------------
def technical_sources(query: str) -> list[Result]:
    def mdn() -> list[Result]:
        response = requests.get("https://developer.mozilla.org/api/v1/search", params={"q": query[:180], "locale": "en-US"}, headers=UA, timeout=15)
        response.raise_for_status()
        return [Result(compact(item.get("title"), 140), f"https://developer.mozilla.org{item.get('mdn_url')}", compact(item.get("summary"), 200), "MDN")
                for item in response.json().get("documents", [])[:2] if item.get("mdn_url")]

    def stack() -> list[Result]:
        response = requests.get("https://api.stackexchange.com/2.3/search/advanced",
                                params={"site": "stackoverflow", "q": query[:180], "sort": "relevance", "order": "desc", "pagesize": 3, "accepted": "True"},
                                headers=UA, timeout=15)
        response.raise_for_status()
        return [Result(compact(item.get("title"), 160), item.get("link", ""), f"{item.get('answer_count', 0)} resp. · score {item.get('score', 0)}", "Stack Overflow")
                for item in response.json().get("items", [])[:3] if item.get("link")]

    def repositories() -> list[Result]:
        response = requests.get("https://api.github.com/search/repositories",
                                params={"q": f"{query[:140]} in:name,description", "sort": "stars", "per_page": 3},
                                headers={**UA, "Accept": "application/vnd.github+json"}, timeout=15)
        if not response.ok:
            return []
        return [Result(compact(item.get("full_name"), 120), item.get("html_url", ""), compact(f"{item.get('description') or ''} · ★{item.get('stargazers_count', 0)}", 200), "GitHub")
                for item in response.json().get("items", [])[:2] if item.get("description")]

    found: list[Result] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in [pool.submit(function) for function in (mdn, stack, repositories)]:
            try:
                found.extend(future.result())
            except Exception:
                continue
    return found[:7]


# ---------------------------------------------------------------------------
# GitHub panel (optional token).
# ---------------------------------------------------------------------------
def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", **UA}
    if config.MICHIFLIN_GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.MICHIFLIN_GITHUB_TOKEN}"
    return headers


def _html_url(api_url: str, repository: str) -> str:
    """Browser URL for a notification subject (API URLs are not readable pages)."""
    if not api_url:
        return f"https://github.com/{repository}"
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    return url.replace("/pulls/", "/pull/").replace("/commits/", "/commit/")


def github_text(argument: str, locale: str) -> str:
    from . import names

    words = [names.word_key(item) for item in argument.strip().lower().replace("_", " ").split()]
    action = words[0] if words else ""
    repository = config.env("GITHUB_REPOSITORY")
    notifications, account = names.cmd(locale, "github", "notifications"), names.cmd(locale, "github", "account")
    try:
        if action in {"notifications", "n"}:
            if not config.MICHIFLIN_GITHUB_TOKEN:
                return tr(locale, "ℹ️ Falta el token de GitHub del bot (permiso notifications).", "ℹ️ The bot's GitHub token is missing (notifications scope).")
            if "read" in words[1:]:
                response = requests.put("https://api.github.com/notifications", json={}, headers=_github_headers(), timeout=20)
                return tr(locale, "✅ Notificaciones marcadas como leídas.", "✅ Notifications marked as read.") if response.status_code in {202, 205} \
                    else tr(locale, f"⚠️ GitHub respondió {response.status_code}.", f"⚠️ GitHub answered {response.status_code}.")
            response = requests.get("https://api.github.com/notifications", params={"all": "true", "per_page": 15}, headers=_github_headers(), timeout=20)
            response.raise_for_status()
            threads = response.json()
            if not threads:
                return tr(locale, "🐙 No hay notificaciones recientes.", "🐙 No recent notifications.")
            unread = sum(1 for thread in threads if thread.get("unread"))
            lines = [tr(locale, f"🐙 Notificaciones · {unread} sin leer", f"🐙 Notifications · {unread} unread"), ""]
            for index, thread in enumerate(threads, 1):
                subject = thread.get("subject") or {}
                full_name = (thread.get("repository") or {}).get("full_name", "")
                mark = "●" if thread.get("unread") else "○"
                when = compact(thread.get("updated_at"), 16).replace("T", " ")
                lines.append(f"{mark} {index}. {compact(subject.get('title'), 140)}\n   {full_name} · {subject.get('type', '')} · {when}\n   "
                             + links.command("v", _html_url(str(subject.get("url") or ""), full_name)))
            lines += ["", tr(locale, "● sin leer · ○ leída", "● unread · ○ read")]
            if unread:
                lines.append(names.cmd(locale, "github", "notifications", "read") + tr(locale, " → marcar todas como leídas", " → mark all as read"))
            return "\n".join(lines)
        if action in {"account", "c"}:
            if not config.MICHIFLIN_GITHUB_TOKEN:
                return tr(locale, "ℹ️ Falta el token de GitHub del bot.", "ℹ️ The bot's GitHub token is missing.")
            response = requests.get("https://api.github.com/user", headers=_github_headers(), timeout=20)
            response.raise_for_status()
            profile = response.json()
            return (f"🐙 {profile.get('login')}\n"
                    + tr(locale, f"Repositorios: {profile.get('public_repos', 0)} públicos · {profile.get('total_private_repos', 0)} privados\n"
                                 f"Seguidores: {profile.get('followers', 0)} · siguiendo: {profile.get('following', 0)}",
                         f"Repositories: {profile.get('public_repos', 0)} public · {profile.get('total_private_repos', 0)} private\n"
                         f"Followers: {profile.get('followers', 0)} · following: {profile.get('following', 0)}"))
        if not repository:
            return f"{account} · {notifications}"
        info = requests.get(f"https://api.github.com/repos/{repository}", headers=_github_headers(), timeout=20).json()
        runs = requests.get(f"https://api.github.com/repos/{repository}/actions/runs", params={"per_page": 3}, headers=_github_headers(), timeout=20).json()
        lines = [f"🐙 {info.get('full_name', repository)} · {info.get('visibility', '')}"]
        for run in (runs.get("workflow_runs") or [])[:3]:
            lines.append(f"• {run.get('status')}{'/' + run['conclusion'] if run.get('conclusion') else ''} · {compact(run.get('created_at'), 16).replace('T', ' ')}")
        lines.append(f"{account} · {notifications}")
        return "\n".join(lines)
    except (requests.RequestException, ValueError, AttributeError):
        return tr(locale, "⚠️ No pude consultar GitHub.", "⚠️ GitHub query failed.")


__all__ = ["search", "video_search", "search_text", "news_text", "weather_text", "rate_text", "calc_text", "wiki_summary",
           "technical_sources", "github_text", "looks_like_math", "calculate", "host_of", "unlinked"]
