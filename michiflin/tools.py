"""Quota-free information tools: search, news, weather, rates, encyclopedia, calculator, GitHub."""
from __future__ import annotations

import ast
import math
import re
import time
import unicodedata
from defusedxml import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote, quote_plus

import requests

from . import config, links, state
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
    51: ("llovizna ligera", "light drizzle"), 53: ("llovizna", "drizzle"), 55: ("llovizna intensa", "heavy drizzle"),
    61: ("lluvia ligera", "light rain"), 63: ("lluvia", "rain"), 65: ("lluvia intensa", "heavy rain"),
    71: ("nieve ligera", "light snow"), 73: ("nieve", "snow"), 75: ("nieve intensa", "heavy snow"),
    80: ("chubascos ligeros", "light showers"), 81: ("chubascos", "showers"), 82: ("chubascos fuertes", "heavy showers"),
    95: ("tormenta", "thunderstorm"), 96: ("tormenta con granizo", "thunderstorm with hail"), 99: ("tormenta fuerte con granizo", "severe thunderstorm"),
}


def weather_text(query: str, locale: str) -> str:
    query = query.strip()
    if not query:
        return tr(locale, "Uso: /clima <ciudad>", "Usage: /weather <city>")
    try:
        geocode = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                               params={"name": query, "count": 1, "language": locale, "format": "json"}, timeout=20)
        geocode.raise_for_status()
        places = geocode.json().get("results") or []
        if not places:
            return tr(locale, f"🌦️ No encontré “{query}”.", f"🌦️ “{query}” was not found.")
        place = places[0]
        forecast = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 3, "timezone": "auto",
        }, timeout=20)
        forecast.raise_for_status()
        body = forecast.json()
    except (requests.RequestException, ValueError, KeyError) as exc:
        return tr(locale, f"⚠️ No pude consultar el clima ({exc.__class__.__name__}).", f"⚠️ Weather lookup failed ({exc.__class__.__name__}).")
    index = 1 if locale == "en" else 0
    now = body.get("current", {})
    label = ", ".join(filter(None, [place.get("name"), place.get("admin1"), place.get("country")]))
    sky = WEATHER.get(int(now.get("weather_code", -1)), ("?", "?"))[index]
    lines = [
        f"🌦️ {label}",
        f"{sky.capitalize()} · {now.get('temperature_2m', '—')} °C ({tr(locale, 'sensación', 'feels')} {now.get('apparent_temperature', '—')} °C)",
        f"{tr(locale, 'Humedad', 'Humidity')} {now.get('relative_humidity_2m', '—')}% · {tr(locale, 'Viento', 'Wind')} {now.get('wind_speed_10m', '—')} km/h · {now.get('precipitation', '—')} mm",
        "",
    ]
    daily = body.get("daily", {})
    for day, code, high, low, rain in zip(daily.get("time", []), daily.get("weather_code", []), daily.get("temperature_2m_max", []),
                                          daily.get("temperature_2m_min", []), daily.get("precipitation_probability_max", [])):
        name = datetime.fromisoformat(day).strftime("%d/%m")
        lines.append(f"{name}: {WEATHER.get(int(code), ('?', '?'))[index]} · {low}–{high} °C · 💧{rain}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# elTOQUE informal exchange rates: official API when a token exists, public table otherwise.
# ---------------------------------------------------------------------------
ELTOQUE_ENDPOINT = "https://tasas.eltoque.com/v1/trmi"
ELTOQUE_PAGE = "https://eltoque.com/"


class RateError(Exception):
    pass


def _public_rates() -> dict[str, float]:
    """Read the rate table shown on elTOQUE's home page."""
    from bs4 import BeautifulSoup

    def run() -> dict[str, float]:
        try:
            response = requests.get(ELTOQUE_PAGE, timeout=25, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Mobile"})
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RateError("connection") from exc
        rates: dict[str, float] = {}
        for row in BeautifulSoup(response.text, "lxml").select("table tr"):
            text = " ".join(row.get_text(" ", strip=True).split())
            match = re.match(r"1\s+([A-Z]{2,6})\b.*?([\d.,]+)\s*CUP", text)
            if match and match.group(1) not in rates:
                rates[match.group(1)] = float(match.group(2).replace(",", ""))
        if not rates:
            raise RateError("empty")
        return rates

    return _cached("public_rates", 600, run)


def _rates() -> dict[str, float]:
    if not config.ELTOQUE_API_TOKEN:
        return _public_rates()

    def run() -> dict[str, float]:
        try:
            response = requests.get(ELTOQUE_ENDPOINT, timeout=25,
                                    headers={"Authorization": f"Bearer {config.ELTOQUE_API_TOKEN}", "Accept": "application/json"})
        except requests.RequestException as exc:
            raise RateError("connection") from exc
        if response.status_code in {401, 422}:
            raise RateError("authorization")
        if response.status_code == 429:
            raise RateError("rate_limit")
        if not response.ok:
            raise RateError("http")
        payload = response.json()
        rates = {}
        for code, value in (payload.get("tasas") or {}).items():
            try:
                number = float(str(value).replace(",", "."))
            except ValueError:
                continue
            if number > 0 and re.fullmatch(r"[A-Z0-9]{2,12}", str(code).upper()):
                rates[str(code).upper()] = number
        if not rates:
            raise RateError("empty")
        return rates

    return _cached("rates", 120, run)


def current_rates() -> dict[str, float]:
    try:
        return _rates()
    except RateError:
        return _public_rates()


def _number(value: float, locale: str) -> str:
    decimals = 6 if 0 < abs(value) < 0.01 else 2
    text = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
    return text.replace(",", "X").replace(".", ",").replace("X", ".") if locale != "en" else text


def rate_text(argument: str, locale: str) -> str:
    usage = tr(locale, "Uso: /tasa · /tasa USD · /tasa 100 USD · /tasa 5000 CUP USD", "Usage: /rate · /rate USD · /rate 100 USD · /rate 5000 CUP USD")
    parts = argument.upper().replace(",", ".").split()
    try:
        rates = _rates()
    except RateError as exc:
        reason = str(exc)
        try:
            rates = _public_rates()
        except (RateError, ValueError):
            if reason == "authorization":
                return tr(locale, "⚠️ elTOQUE rechazó el token configurado.", "⚠️ elTOQUE rejected the configured token.")
            if reason == "rate_limit":
                return tr(locale, "⏳ elTOQUE alcanzó su límite de consultas; prueba en un minuto.", "⏳ elTOQUE rate limit reached; try in a minute.")
            return tr(locale, "⚠️ No pude consultar elTOQUE ahora.", "⚠️ elTOQUE is unavailable right now.")
    except (ValueError, requests.RequestException):
        return tr(locale, "⚠️ elTOQUE devolvió datos inválidos.", "⚠️ elTOQUE returned invalid data.")
    footer = tr(locale, f"elTOQUE · mercado informal · {local_time(time.time())}", f"elTOQUE · informal market · {local_time(time.time())}")
    if not parts:
        preferred = ["USD", "EUR", "MLC", "ZELLE", "USDT", "CAD", "MXN", "BTC", "TRX"]
        ordered = [code for code in preferred if code in rates] + sorted(code for code in rates if code not in preferred)
        return "\n".join([tr(locale, "💱 CUP por unidad", "💱 CUP per unit")] + [f"{code}: {_number(rates[code], locale)}" for code in ordered] + ["", footer])
    try:
        if len(parts) == 1:
            amount, source, target = 1.0, parts[0], "CUP"
        elif len(parts) == 2:
            amount, source, target = float(parts[0]), parts[1], "CUP"
        else:
            amount, source, target = float(parts[0]), parts[1], parts[2]
    except ValueError:
        return usage
    value = {"CUP": 1.0, **rates}
    if source not in value or target not in value:
        return tr(locale, f"⚠️ Sin tasa para {source if source not in value else target}.", f"⚠️ No rate for {source if source not in value else target}.")
    converted = amount * value[source] / value[target]
    return f"💱 {_number(amount, locale)} {source} ≈ {_number(converted, locale)} {target}\n\n{footer}"


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
