"""Plain-message agent: detects intent and answers with the cheapest adequate tool.

Order: local replies → URLs → images → calculator/weather/rates/news → encyclopedia →
web search for current facts → AI with a role-matched model. Search results are used
before AI where possible so free AI quotas last longer.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import ai, answer, chat, config, documents, extras, images, links, names, pager, progress, state, tools, web
from .util import compact, first_url, local_today, tr

GREETINGS = re.compile(r"^(hola+|buenas( tardes| noches| d[ií]as)?|buenos d[ií]as|hey|hi|hello|ey|saludos|qu[eé] tal|klk)[\s!.,😀-🙏]*$", re.I)
CAPABILITIES = re.compile(r"^(qu[eé] (puedes|sabes) hacer|qu[eé] haces|para qu[eé] sirves|c[oó]mo funcionas|qui[eé]n eres|ayuda|help|"
                          r"what can you do|what do you do|who are you|how do you work)[\s?!.]*$", re.I)
THANKS = re.compile(r"^(gracias|muchas gracias|thanks|thank you|ok|okay|vale|listo|perfecto|genial|👍|🙏|❤️)[\s!.]*$", re.I)
WEATHER = re.compile(r"\b(clima|tiempo|temperatura|pron[oó]stico|llover[aá]?|lluvia|weather|forecast)\b.*?\b(en|de|para|in|for)\s+([\w\sáéíóúñ,.-]{2,60})\??$", re.I)
CURRENCY = {"dolar": "USD", "dólar": "USD", "dolares": "USD", "dólares": "USD", "usd": "USD", "euro": "EUR", "euros": "EUR",
            "eur": "EUR", "mlc": "MLC", "cup": "CUP", "pesos": "CUP", "peso": "CUP", "zelle": "ZELLE", "usdt": "USDT",
            "cad": "CAD", "mxn": "MXN"}
CONVERT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(CURRENCY) + r")\b.*?\b(?:en|a|to|in)\s+(" + "|".join(CURRENCY) + r")\b", re.I)
RATE = re.compile(r"\b(tasa|tasas|cambio|precio|cu[aá]nto est[aá]|a c[oó]mo est[aá]|vale)\b.*\b(d[oó]lar|usd|euro|eur|mlc|usdt|divisa)", re.I)
NEWS = re.compile(r"^(noticias|[uú]ltimas noticias|titulares|news|headlines)\b\s*(de|sobre|about|on)?\s*(.*)$", re.I)
VIDEO_SEARCH = re.compile(r"^(?:(?:busca(?:r|me)?|b[uú]scame|encuentra(?:me)?|dame|mu[eé]strame|quiero(?: ver| escuchar)?|pon(?:me)?|find|show me|search(?: for)?)\s+)?"
                          r"(?:(?:unos?|unas|los|las|algunos|el|la|some|the)\s+)?(?:videos?|v[ií]deos?|clips?|canci[oó]n(?:es)?|m[uú]sica|songs?|music)\s+"
                          r"(?:de|del|sobre|con|of|about|by|from)\s+(.{2,120}?)(?:\s+(?:en|on)\s+youtube)?[?.!]*$", re.I)
YOUTUBE_SEARCH = re.compile(r"^(?:busca(?:r|me)?|b[uú]scame|search)?\s*(?:en|on)\s+youtube\s+(.{2,120})$|^(.{2,120}?)\s+(?:en|on)\s+youtube[?.!]*$", re.I)
SEARCH = re.compile(r"^(busca|buscar|b[uú]scame|investiga|search|look up|google)\s+(.+)$", re.I)
DEFINITION = re.compile(r"^(qu[eé] es|qu[eé] son|qui[eé]n es|qui[eé]n fue|qui[eé]nes son|qu[eé] significa|definici[oó]n de|what is|what are|who is|who was|define)\s+(.{2,80}?)\??$", re.I)
CURRENT = re.compile(r"\b(hoy|ahora|actual|actualmente|[uú]ltim[oa]s?|reciente|esta semana|este mes|este a[nñ]o|202[5-9]|precio|cotizaci[oó]n|resultado|marcador|qui[eé]n gan[oó]|cu[aá]ndo (es|ser[aá]|juega)|today|now|current|latest|recent|price|score|who won|this week)\b", re.I)
CODE = re.compile(r"```|\b(python|javascript|typescript|java|kotlin|c\+\+|c#|golang|rust|php|sql|bash|regex|html|css|react|node|django|flask|api|json|error|traceback|exception|bug|compil|funci[oó]n|function|script|c[oó]digo|code|programa|algoritmo|git)\b", re.I)
REASON = re.compile(r"\b(explica|explicame|expl[ií]came|por qu[eé]|analiza|compara|eval[uú]a|demuestra|resuelve|razona|calcula|plan|estrategia|ventajas|desventajas|explain|why|analy[sz]e|compare|prove|solve|reason|pros and cons)\b", re.I)
TRANSLATE = re.compile(r"^(?:traduce(?:me)?|traducir|tradúceme|translate)\s+(?:(?:al?|en|to|into)\s+)?(ingl[eé]s|espa[nñ]ol|franc[eé]s|portugu[eé]s|"
                       r"italiano|alem[aá]n|ruso|chino|japon[eé]s|english|spanish|french|portuguese|italian|german|russian|chinese|japanese)\s*:?\s+(.+)$|"
                       r"^(?:traduce(?:me)?|traducir|tradúceme|translate)\s+(.+?)\s+(?:al?|en|to|into)\s+(ingl[eé]s|espa[nñ]ol|franc[eé]s|portugu[eé]s|italiano|"
                       r"alem[aá]n|ruso|chino|japon[eé]s|english|spanish|french|portuguese|italian|german|russian|chinese|japanese)[.!]*$", re.I | re.S)
UNITS = re.compile(r"^(?:convierte|convertir|pasa|cu[aá]nto(?:s|as)? (?:son|es)|convert|how many \w+ (?:is|are))?\s*(-?\d+(?:[.,]\d+)?\s*[^\s\d]+)\s+"
                   r"(?:a|en|to|in)\s+([^\s?]+)\s*\??$", re.I)
CLOCK = re.compile(r"^(?:qu[eé] hora es|hora)\s+(?:en|in)\s+(.{2,60}?)\??$|^what time is it in\s+(.{2,60}?)\??$", re.I)
SUMMARY_URL = re.compile(r"\b(resume|resumen|resumir|res[uú]meme|qu[eé] dice|explica|traduce|summari[sz]e|summary|translate|tl;?dr)\b", re.I)


@dataclass
class Memory:
    turns: deque = field(default_factory=lambda: deque(maxlen=8))
    last_query: str = ""
    last_kind: str = ""
    last_results: list[Any] = field(default_factory=list)
    document: Any = None
    document_at: float = 0.0
    updated: float = field(default_factory=time.time)


_memory: dict[int, Memory] = {}
_lock = threading.Lock()


def memory(chat_id: int) -> Memory:
    with _lock:
        item = _memory.get(chat_id)
        if item is None or time.time() - item.updated > 3600:
            item = Memory()
            _memory[chat_id] = item
        item.updated = time.time()
        return item


def forget(chat_id: int) -> None:
    with _lock:
        _memory.pop(chat_id, None)


def system_prompt(locale: str) -> str:
    language = "English" if locale == "en" else "Spanish (neutral, Cuban-friendly)"
    now = datetime.now(config.LOCAL_ZONE).strftime("%Y-%m-%d %H:%M")
    return (f"{config.AI_SYSTEM_PROMPT}\nCurrent date and time: {now} ({config.TIMEZONE_NAME}). Reply in {language}. "
            "Plain text for a chat app: prefer readable paragraphs and lists, and at most light *bold*. "
            "Give the detail the task needs; do not impose an arbitrary word limit.")


def pick_role(text: str) -> str:
    if len(text) > 3500:
        return "long"
    if CODE.search(text):
        return "code"
    if REASON.search(text) or len(text) > 600:
        return "reason"
    return "fast" if len(text) < 80 else "chat"


def ask(chat_id: int, text: str, locale: str, role: str = "", context: str = "", remember: bool = True) -> str:
    item = memory(chat_id)
    messages = [{"role": "system", "content": system_prompt(locale)}]
    for turn_role, content in item.turns:
        messages.append({"role": turn_role, "content": content})
    prompt = f"{context}\n\n{text}".strip() if context else text
    if item.document is not None and time.time() - item.document_at < 1800 and not context:
        prompt = documents.prompt(text, item.document)
        role = role or "long"
    messages.append({"role": "user", "content": prompt})
    result = ai.complete(messages, role or pick_role(text))
    if not result.ok:
        return ai.describe_error(result.error, locale, result)
    if remember:
        item.turns.append(("user", compact(text, 1500)))
        item.turns.append(("assistant", compact(result.text, 1500)))
    item.last_query, item.last_kind = text, "ai"
    return f"{result.text}\n\n— {result.signature()}"


def reply(chat_id: int, query: str, locale: str, results: list[tools.Result] | None = None, limit: int = 4) -> str:
    """Model-free answer: encyclopedia, search results and the top pages, ranked locally into a few cited sentences."""
    progress.phase("🔎 Buscando y leyendo fuentes…", "🔎 Searching and reading sources…")
    sources = answer.gather(query, locale, results, pages=3 if limit <= 4 else 5)
    state.check_work_allowed()
    text, cited = answer.compose(query, sources, locale, limit)
    if not text and results is None:  # keyless search fails now and then; one more pass with the bare topic
        sources = answer.gather(answer.topic(query), locale, None, pages=3)
        text, cited = answer.compose(query, sources, locale, limit)
    item = memory(chat_id)
    item.last_query, item.last_kind = query, "answer"
    if not text:
        hits = results if results is not None else []
        if hits:
            entries = "\n".join(links.entry(index, hit.title, hit.url, hit.snippet) for index, hit in enumerate(hits[:5], 1))
            return tr(locale, "🔎 No encontré una respuesta directa. Estas fuentes pueden ayudar:\n\n",
                      "🔎 No direct answer found. These sources may help:\n\n") + entries
        return tr(locale, "🤔 No encontré información clara sobre eso. Prueba con otras palabras o con /buscar.",
                  "🤔 I found no clear information about that. Try other words or /search.")
    tail = "\n".join(f"[{index}] {compact(source.title, 70)} · {links.command('v', source.url)}" for index, source in enumerate(cited, 1))
    more_hint = names.cmd(locale, "more") + tr(locale, " → más detalle", " → more detail")
    return f"💡 {text}\n\n{tail}\n\n{more_hint}".replace("\n\n\n\n", "\n\n")


SEARCH_RESULTS, PAGE_SIZE = 20, 5


def _search_reply(chat_id: int, query: str, locale: str) -> str:
    item = memory(chat_id)
    progress.phase("🔎 Buscando en la web…", "🔎 Searching the web…")
    try:
        results = tools.search(query, locale, SEARCH_RESULTS)
    except Exception:
        results = []
    item.last_query, item.last_kind, item.last_results = query, "search", results
    if not results:
        return reply(chat_id, query, locale, [])
    entries = [links.entry(index, result.title, result.url, result.snippet) for index, result in enumerate(results, 1)]
    return pager.show(chat_id, pager.Pager(f"🔎 {compact(query, 80)}", pager.chunk(entries, PAGE_SIZE), locale, "search",
                                           tr(locale, "💡 /resumen → respuesta extraída de estos resultados",
                                              "💡 /summary → answer extracted from these results")))


def video_reply(chat_id: int, query: str, locale: str, kind: str = "video", quality: int = 0) -> str:
    """Video search list with tap commands to download as video or audio."""
    progress.phase("🎬 Buscando videos…", "🎬 Searching videos…")
    results = tools.video_search(query, locale)
    if not results:
        return tr(locale, "🎬 Sin resultados de video. Prueba otras palabras o /buscar.", "🎬 No video results. Try other words or /search.")
    item = memory(chat_id)
    item.last_query, item.last_kind, item.last_results = query, "videos", results
    video_suffix = str(quality) if kind == "video" and quality else ""
    audio_suffix = str(quality) if kind == "audio" and quality else ""
    entries = []
    for index, result in enumerate(results, 1):
        video = links.command("vid", result.url, video_suffix)
        audio = links.command("a", result.url, audio_suffix)
        actions = f"🎧 {audio} · 🎬 {video}" if kind == "audio" else f"🎬 {video} · 🎧 {audio}"
        entries.append(f"{index}. {compact(result.title, 110)}\n   {result.snippet}\n   {actions} · ⚙️ {links.command('info', result.url)}")
    tail = tr(locale, "🎬 video · 🎧 audio · ⚙️ calidades y tamaños", "🎬 video · 🎧 audio · ⚙️ qualities and sizes")
    return pager.show(chat_id, pager.Pager(f"🎬 {compact(query, 80)}", pager.chunk(entries, PAGE_SIZE), locale, "videos", tail))


def summary(chat_id: int, locale: str) -> str:
    """Answer extracted from the last search results."""
    item = memory(chat_id)
    if not item.last_query or item.last_kind not in {"search", "wiki", "videos", "answer"}:
        return tr(locale, "ℹ️ Primero haz una búsqueda con /buscar.", "ℹ️ Run a /search first.")
    return reply(chat_id, item.last_query, locale, (item.last_results or [])[:8] or None)


def more(chat_id: int, locale: str) -> str:
    """More of the last thing: the next page of a list, else a longer answer from more sources."""
    current = pager.current(chat_id)
    item = memory(chat_id)
    if current is not None and current.kind in {"search", "videos"} and item.last_kind == current.kind:
        if current.has_next():
            return pager.move(chat_id, 1, locale)
        return summary(chat_id, locale) if current.kind == "search" else tr(locale, "ℹ️ No hay más resultados.", "ℹ️ No more results.")
    if not item.last_query:
        return tr(locale, "ℹ️ No hay una consulta anterior para ampliar.", "ℹ️ There is no previous query to expand.")
    return reply(chat_id, item.last_query, locale, item.last_results or None, limit=8)


def send_image(chat_id: int, prompt: str, locale: str) -> None:
    result = images.generate(prompt)
    if result.error == "person_quota":
        from . import usage

        chat.text(chat_id, "⚠️ " + usage.limit_text("image", locale))
        return
    if not result.data:
        reason = {"no_source": tr(locale, "no hay generadores configurados", "no generators are configured"),
                  "quota": tr(locale, "las cuotas gratuitas de imagen se agotaron por hoy", "free image quotas are used up for today")}.get(
            result.error, tr(locale, "los generadores no respondieron", "the generators did not answer"))
        chat.text(chat_id, f"⚠️ {tr(locale, 'Imagen', 'Image')}: {reason}.")
        return
    target = config.TEMP_ROOT / f"img-{images.stamp()}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.data)
    chat.send(chat_id, f"🎨 {compact(images.clean_prompt(prompt), 160)}\n— {result.source}", file=target, filename=target.name, viewtype="Image")


def handle(chat_id: int, text: str, locale: str) -> None:
    """Route one plain-text message through free tools only; models and their keys are never touched."""
    with ai.isolated():
        _handle(chat_id, text, locale)


def _handle(chat_id: int, text: str, locale: str) -> None:
    address = (chat.sender() or "").strip().lower()
    if address and state.get_kill_switch(address):
        chat.text(chat_id, tr(locale, "🛑 Kill switch activo. Usa /kill_switch no para reanudar.",
                              "🛑 Kill switch is on. Use /kill_switch no to resume."))
        return
    stripped = text.strip().lstrip("¿¡").strip()
    if GREETINGS.match(stripped):
        chat.text(chat_id, tr(locale, "👋 ¡Hola! Pregúntame lo que quieras o pega un enlace. Busco, leo las fuentes y te traigo lo esencial.",
                              "👋 Hi! Ask me anything or paste a link. I search, read the sources and bring you the essentials."))
        return
    if CAPABILITIES.match(stripped):
        chat.text(chat_id, tr(
            locale,
            "Soy el agente de Michiflin AI. Trabajo solo con herramientas gratuitas, sin ningún modelo de IA ni sus cuotas:\n"
            "- Busco en la web, leo las fuentes y extraigo las frases clave.\n- Explico conceptos con Wikipedia.\n"
            "- Resumo y traduzco páginas.\n- Calculo y convierto monedas, unidades y horas.\n"
            "- Consulto el clima, la tasa informal y las noticias.\n- Abro enlaces y busco videos.\n\n"
            "Para conversar, redactar o programar, usa Automático o un modelo en la pestaña IA.",
            "I am the Michiflin AI agent. I only use free tools, never an AI model or its quota:\n"
            "- I search the web, read the sources and extract the key sentences.\n- I explain concepts with Wikipedia.\n"
            "- I summarize and translate pages.\n- I calculate and convert currencies, units and time zones.\n"
            "- I check the weather, the informal exchange rate and the news.\n- I open links and find videos.\n\n"
            "To chat, write or code, use Automatic or a model in the AI tab."))
        return
    if THANKS.match(stripped):
        chat.text(chat_id, tr(locale, "😺 ¡A la orden!", "😺 You're welcome!"))
        return
    url = first_url(stripped)
    if url:
        rest = stripped.replace(url, "").strip()
        if rest and SUMMARY_URL.search(rest):
            summarize_url(chat_id, url, rest, locale)
        else:
            web.route(chat_id, url, locale)
        return
    if images.is_image_request(stripped):
        send_image(chat_id, stripped, locale)
        return
    match = TRANSLATE.match(stripped)
    if match:
        language, text = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
        chat.text(chat_id, extras.translate_text(f"{language} {text}", locale))
        return
    match = UNITS.match(stripped)
    if match and len(stripped) < 80:
        converted = extras.convert_text(f"{match.group(1)} a {match.group(2)}", locale)
        if converted.startswith("📏"):
            chat.text(chat_id, converted)
            return
    match = CLOCK.match(stripped)
    if match:
        chat.text(chat_id, extras.time_text(match.group(1) or match.group(2), locale))
        return
    if tools.looks_like_math(stripped):
        chat.text(chat_id, tools.calc_text(stripped, locale))
        return
    match = WEATHER.search(stripped)
    if match and len(stripped) < 120:
        chat.text(chat_id, tools.weather_text(match.group(3).strip(" ?.,"), locale))
        return
    match = CONVERT.search(stripped)
    if match and len(stripped) < 120 and "CUP" in (CURRENCY[match.group(2).lower()], CURRENCY[match.group(3).lower()]):
        amount = match.group(1).replace(",", ".")
        chat.text(chat_id, tools.rate_text(f"{amount} {CURRENCY[match.group(2).lower()]} {CURRENCY[match.group(3).lower()]}", locale))
        return
    if RATE.search(stripped) and len(stripped) < 120 and (re_cuba(stripped) or "cup" in stripped.lower()):
        chat.text(chat_id, tools.rate_text("", locale))
        return
    match = NEWS.match(stripped)
    if match and len(stripped) < 100:
        chat.text(chat_id, tools.news_text(match.group(3), locale))
        return
    match = VIDEO_SEARCH.match(stripped) or YOUTUBE_SEARCH.match(stripped)
    if match and len(stripped) < 160:
        query = next(group for group in match.groups() if group)
        chat.text(chat_id, video_reply(chat_id, query.strip(" ?.,"), locale))
        return
    match = SEARCH.match(stripped)
    if match:
        chat.text(chat_id, _search_reply(chat_id, match.group(2), locale))
        return
    match = DEFINITION.match(stripped)
    if match and not CURRENT.search(stripped):
        summary = tools.wiki_summary(match.group(2), locale)
        if summary and answer._about(summary[0], stripped):
            title, extract, page_url = summary
            item = memory(chat_id)
            item.last_query, item.last_kind, item.last_results = stripped, "wiki", []
            chat.text(chat_id, f"📚 {title}\n\n{extract}\n\n{links.command('v', page_url)} · {names.cmd(locale, 'more')}")
            return
    chat.text(chat_id, reply(chat_id, stripped, locale))


def re_cuba(text: str) -> bool:
    return bool(re.search(r"\b(cuba|cup|informal|el ?toque|hoy)\b", text, re.I))


def summarize_url(chat_id: int, url: str, instruction: str, locale: str) -> None:
    from . import reader

    try:
        page = reader.load(url)
    except Exception as exc:
        chat.text(chat_id, web.url_error(exc, locale))
        return
    progress.phase("📄 Resumiendo la página…", "📄 Summarizing the page…")
    points = answer.summarize(answer.page_text(page), 7)
    if not points:
        chat.text(chat_id, tr(locale, "⚠️ La página no tiene texto suficiente para resumir.", "⚠️ The page has too little text to summarize.")
                  + f"\n{links.command('v', page.url)}")
        return
    body = "\n".join(f"- {compact(point, 400)}" for point in points)
    if re.search(r"tradu|translat", instruction, re.I):
        match = re.search(r"\b(?:al?|en|to|into)\s+(\w+)\s*$", instruction, re.I)
        target = (extras.language_code(match.group(1)) if match else "") or ("en" if locale == "en" else "es")
        translated, _source = extras.translate(body, target)
        body = translated or body
    chat.text(chat_id, f"📄 {compact(page.title, 120)}\n\n{body}\n\n{links.command('v', page.url)}")


def handle_attachment(chat_id: int, message: Any, caption: str, locale: str) -> None:
    """Voice notes are transcribed, images analyzed, documents previewed or analyzed."""
    name = str(getattr(message, "file_name", "") or "")
    mime = str(getattr(message, "file_mime", "") or "")
    viewtype = str(getattr(getattr(message, "view_type", ""), "value", getattr(message, "view_type", "")))
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    path = getattr(message, "file", None)
    if viewtype in {"Voice", "Audio"} or suffix in documents.AUDIO_SUFFIXES or mime.startswith("audio/"):
        try:
            data = open(path, "rb").read() if path else b""
        except OSError:
            data = b""
        if not data or len(data) > 25 * 1024 * 1024:
            chat.text(chat_id, tr(locale, "⚠️ No pude leer el audio (máx. 25 MB).", "⚠️ Could not read the audio (max 25 MB)."))
            return
        progress.phase("🎤 Transcribiendo el audio…", "🎤 Transcribing the audio…")
        transcript = ai.transcribe(data, name or "voice.ogg", mime or "audio/ogg")
        if not transcript:
            chat.text(chat_id, tr(locale, "⚠️ No pude transcribir el audio.", "⚠️ Could not transcribe the audio."))
            return
        if caption:
            chat.text(chat_id, f"🎤 «{compact(transcript, 3000)}»\n\n" + ask(chat_id, f"{caption}\n\n{transcript}", locale))
            return
        if viewtype == "Voice":
            spoken_locale = locale
            chat.text(chat_id, f"🎤 «{compact(transcript, 600)}»")
            handle(chat_id, transcript, spoken_locale)
            return
        chat.text(chat_id, f"🎤 {compact(transcript, 3800)}")
        return
    if viewtype in {"Image", "Gif"} or suffix in documents.IMAGE_SUFFIXES or mime.startswith("image/"):
        try:
            data = open(path, "rb").read() if path else b""
        except OSError:
            data = b""
        if not data:
            chat.text(chat_id, tr(locale, "⚠️ La imagen aún no está disponible.", "⚠️ The image is not available yet."))
            return
        question = caption or tr(locale, "Describe la imagen brevemente y transcribe cualquier texto visible.", "Briefly describe the image and transcribe any visible text.")
        messages = [{"role": "system", "content": system_prompt(locale)}, {"role": "user", "content": question}]
        result = ai.complete(messages, "vision", images=[(mime or "image/jpeg", images.compress(data, 1600, 85))])
        chat.text(chat_id, f"{result.text}\n\n— {result.signature()}" if result.ok else ai.describe_error(result.error, locale, result))
        return
    progress.phase("📑 Leyendo el documento…", "📑 Reading the document…")
    try:
        document = documents.extract(message)
    except documents.DocumentError as exc:
        chat.text(chat_id, documents.error_text(exc, locale))
        return
    item = memory(chat_id)
    item.document, item.document_at = document, time.time()
    if caption:
        chat.text(chat_id, ask(chat_id, caption, locale, role="long"))
        return
    chat.text(chat_id, documents.preview(document, locale) + "\n\n" + tr(locale, "💬 Pregúntame sobre este documento durante 30 min.", "💬 Ask me about this document for 30 min."))


def today() -> str:
    return local_today()
