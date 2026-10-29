"""Command parsing and handlers. Tap-friendly forms use underscores: /vid_k3_360.

Handlers are registered under canonical keys (see names.py); Spanish and English spellings
are both accepted, and every reply shows commands in the reader's language only.
"""
from __future__ import annotations

import secrets
import shutil
import time
from typing import Any, Callable

from . import __version__, access, admin, agent, ai, browser, chat, config, extras, images, links, media, names, pager, queue, state, tools, usage, web
from .util import duration_text, first_url, human_size, is_url, local_time, span_text, tr

Handler = Callable[[int, list[str], str, str], None]
COMMANDS: dict[str, Handler] = {}
ALIASES: dict[str, str] = {}
ADMIN_ONLY: set[str] = set()
_pending: dict[int, tuple[str, float]] = {}


def C(locale: str, key: str, *args: object) -> str:
    return names.cmd(locale, key, *args)


# ---------------------------------------------------------------------------
# Settings: internal key -> (Spanish name, English name, parser, Spanish options, English options).
# ---------------------------------------------------------------------------
YES = {"si", "sí", "yes", "on", "1", "true"}
NO = {"no", "off", "0", "false"}


def _choice(mapping: dict[str, Any]) -> Callable[[str], Any]:
    return lambda value: mapping.get(value)


def _int(value: str) -> Any:
    return int(value) if value.isdigit() else None


def _window(value: str) -> Any:
    if value in NO or value in {"", "siempre", "always"}:
        return ""
    digits = value.replace(":", "").replace(".", "").replace("_", "-")
    if len(digits) == 9 and digits[4] == "-":
        start, end = digits[:4], digits[5:]
        if start.isdigit() and end.isdigit() and int(start[:2]) < 24 and int(end[:2]) < 24:
            return f"{start[:2]}:{start[2:]}-{end[:2]}:{end[2:]}"
    return None


ON_OFF = {**{word: "on" for word in YES}, **{word: "off" for word in NO}}
BOOL = {**{word: True for word in YES}, **{word: False for word in NO}}
LEVELS_ES, LEVELS_EN = "baja · media · alta", "low · medium · high"
SETTINGS: dict[str, tuple[str, str, Callable[[str], Any], str, str]] = {
    "language": ("idioma", "language", _choice({"auto": "auto", "es": "es", "en": "en"}), "auto · es · en", "auto · es · en"),
    "video_height": ("video", "video", lambda v: media.resolve_quality("video", v, 0) or None,
                     f"144 · 240 · 360 · 480 · 720 · 1080 · {LEVELS_ES}", f"144 · 240 · 360 · 480 · 720 · 1080 · {LEVELS_EN}"),
    "audio_kbps": ("audio", "audio", lambda v: media.resolve_quality("audio", v, 0) or None,
                   f"32 · 48 · 64 · 96 · 128 · 160 · {LEVELS_ES}", f"32 · 48 · 64 · 96 · 128 · 160 · {LEVELS_EN}"),
    "audio_format": ("formato", "format", _choice({"m4a": "m4a", "mp3": "mp3", "opus": "opus"}), "m4a · mp3 · opus", "m4a · mp3 · opus"),
    "part_mb": ("parte", "part", _int, "1–20 MB", "1–20 MB"),
    "part_format": ("empaque", "packing", _choice({"raw": "raw", "partes": "raw", "parts": "raw", "7z": "7z", "zip": "zip"}),
                    "partes · 7z · zip", "parts · 7z · zip"),
    "batch": ("lote", "batch", _int, "0 = todas · N partes y pausa", "0 = all · N parts then pause"),
    "delay": ("pausa", "delay", _int, "segundos entre partes", "seconds between parts"),
    "window": ("ventana", "window", _window, "2200-0600 · no", "2200-0600 · no"),
    "auto_send": ("auto", "auto", _choice(BOOL), "si · no", "yes · no"),
    "daily_cap_mb": ("tope", "cap", _int, "MB por 24 h · 0 = sin tope", "MB per 24 h · 0 = no cap"),
    "reader_kb": ("pagina", "page", _int, "30–900 KB", "30–900 KB"),
    "reader_images": ("imagenes", "images", _choice({"no": "off", "off": "off", "baja": "low", "low": "low", "si": "low", "yes": "low"}),
                      "no · baja", "no · low"),
    "reader_links": ("enlaces", "links", _choice({"chat": "chat", "web": "web"}), "chat · web", "chat · web"),
    "capture_device": ("captura", "capture", _choice({"movil": "mobile", "móvil": "mobile", "mobile": "mobile", "pc": "desktop", "desktop": "desktop"}),
                       "movil · pc", "mobile · desktop"),
    "capture_quality": ("nitidez", "sharpness", _choice({"normal": "normal", "alta": "high", "high": "high", "maxima": "max", "máxima": "max", "max": "max"}),
                        "normal · alta · maxima", "normal · high · max"),
    "notify": ("avisos", "notices", _choice({"minimo": "minimal", "mínimo": "minimal", "minimal": "minimal", "normal": "normal"}),
               "minimo · normal", "minimal · normal"),
    "progress": ("progreso", "progress", _choice(ON_OFF), "si · no", "yes · no"),
    "reactions": ("reacciones", "reactions", _choice({**ON_OFF, "todas": "all", "all": "all"}), "si · no · todas", "yes · no · all"),
    "image_size": ("imagen", "imagesize", _int, "512 · 768 · 1024", "512 · 768 · 1024"),
    "ttl_hours": ("retencion", "retention", _int, "horas", "hours"),
    "chat_mode": ("chat", "chat", _choice({"app": "app", "full": "full", "completo": "full"}), "app · completo", "app · full"),
    "user_jobs": ("trabajos", "jobs", _int, "1–20 por persona", "1–20 per person"),
    "user_daily_mb": ("cuota", "quota", _int, "MB por persona y día · 0 = sin cuota", "MB per person a day · 0 = none"),
    "ai_daily": ("consultas", "airequests", _int, "consultas de IA por persona y día · 0 = sin límite", "AI requests per person a day · 0 = no limit"),
    "image_daily": ("dibujos", "imagesday", _int, "imágenes por persona y día · 0 = sin límite", "images per person a day · 0 = no limit"),
    "code_daily": ("proyectos", "projects", _int, "proyectos del Programador por persona y día · 0 = sin límite", "Coder projects per person a day · 0 = no limit"),
    "ai_reserve": ("reserva", "reserve", _int, "% de IA reservado a la administración · 0 = sin reserva", "% of AI kept for administration · 0 = none"),
}
GROUPS = [
    (("Descargas", "Downloads"), ["video_height", "audio_kbps", "audio_format", "part_mb", "part_format"]),
    (("Envío", "Delivery"), ["auto_send", "batch", "delay", "window", "daily_cap_mb"]),
    (("Web", "Web"), ["reader_kb", "reader_images", "reader_links", "capture_device", "capture_quality"]),
    (("General", "General"), ["language", "notify", "progress", "reactions", "image_size", "chat_mode"]),
    (("Para todos (administración)", "For everyone (administration)"), ["user_jobs", "user_daily_mb", "ai_daily", "image_daily", "code_daily", "ai_reserve", "ttl_hours"]),
]
DISPLAY = {"app": ("app", "app"), "full": ("completo", "full"), "off": ("no", "no"),
           "on": ("si", "yes"), "low": ("baja", "low"), "mobile": ("movil", "mobile"), "desktop": ("pc", "desktop"),
           "minimal": ("minimo", "minimal"), "normal": ("normal", "normal"), "all": ("todas", "all"), "raw": ("partes", "parts"),
           "high": ("alta", "high"), "max": ("maxima", "max")}


def setting_name(locale: str, key: str) -> str:
    entry = SETTINGS[key]
    return entry[1] if locale == "en" else entry[0]


def setting_key(typed: str) -> str:
    for key, entry in SETTINGS.items():
        if typed in (entry[0], entry[1]):
            return key
    return ""


def display(key: str, locale: str) -> str:
    value = state.setting(key)
    if isinstance(value, bool):
        return tr(locale, "si", "yes") if value else "no"
    if isinstance(value, str) and value in DISPLAY:
        pair = DISPLAY[value]
        return pair[1] if locale == "en" else pair[0]
    return str(value) if value not in {"", None} else "no"


# ---------------------------------------------------------------------------
# Registry and dispatch.
# ---------------------------------------------------------------------------
def command(key: str, *extra: str, owner: bool = False) -> Callable[[Handler], Handler]:
    """Register a handler under a canonical key, its localized names and extra aliases."""
    def register(function: Handler) -> Handler:
        COMMANDS[key] = function
        spellings = list(names.COMMANDS.get(key, (key, key))) + list(extra)
        for spelling in spellings:
            ALIASES.setdefault(spelling, key)
        if owner:
            ADMIN_ONLY.add(key)
        return function
    return register


def parse(text: str) -> tuple[str, list[str], str] | None:
    """Return (key, underscore_args, rest) or None when text is not a command."""
    if not text.startswith("/") or len(text) < 2:
        return None
    head, _, rest = text[1:].partition(" ")
    head = head.split("@", 1)[0].lower()
    pieces = head.split("_")
    for size in range(len(pieces), 0, -1):
        key = ALIASES.get("_".join(pieces[:size]))
        if key in COMMANDS:
            return key, pieces[size:], rest.strip()
    return "", pieces, rest.strip()


def is_app_command(text: str) -> bool:
    parsed = parse(text.strip())
    return bool(parsed) and parsed[0] == "app"


def set_pending(chat_id: int, key: str) -> None:
    _pending[chat_id] = (key, time.time())


def take_pending(chat_id: int) -> str:
    entry = _pending.pop(chat_id, None)
    return entry[0] if entry and time.time() - entry[1] < 900 else ""


def dispatch(chat_id: int, text: str, locale: str) -> bool:
    parsed = parse(text)
    if parsed is None:
        pending = take_pending(chat_id)
        if pending:
            COMMANDS[pending](chat_id, [], text.strip(), locale)
            return True
        return False
    _pending.pop(chat_id, None)
    key, args, rest = parsed
    if not key or (key in ADMIN_ONLY and not admin.is_admin(chat.sender())):
        chat.text(chat_id, tr(locale, f"⚠️ Comando desconocido. {C(locale, 'help')}", f"⚠️ Unknown command. {C(locale, 'help')}"))
        return True
    COMMANDS[key](chat_id, args, rest, locale)
    return True


def _target(args: list[str], rest: str) -> str:
    """A URL from the text or a link token from the command (/v_k3)."""
    url = first_url(rest)
    if url:
        return url
    for token in args:
        found = links.resolve(token)
        if found:
            return found
    return ""


def _ask(chat_id: int, key: str, locale: str, es: str, en: str) -> None:
    """Wait for the missing argument in the next message."""
    set_pending(chat_id, key)
    chat.text(chat_id, tr(locale, es, en))


def _text(args: list[str], rest: str) -> str:
    return rest or " ".join(args)


# ---------------------------------------------------------------------------
# Help: short pages, each language with its own command names.
# ---------------------------------------------------------------------------
def help_pages(locale: str, owner: bool) -> list[str]:
    def c(key: str, *args: object) -> str:
        return C(locale, key, *args)

    def t(es: str, en: str) -> str:
        return tr(locale, es, en)

    sections: list[tuple[str, list[str]]] = [
        (t("🌐 Web y búsqueda", "🌐 Web and search"), [
            f"{c('web')} URL – " + t("vista ligera (lo normal al enviar un enlace)", "light reader view (default for links)"),
            f"{c('pdf')} URL – " + t("PDF con enlaces e imágenes", "printable PDF with links and images"),
            f"{c('capture')} URL – " + t("captura nítida de la página completa", "sharp full-page screenshot"),
            f"{c('search')} " + t("texto – resultados web, 5 por página", "text – web results, 5 per page"),
            f"{c('videos')} " + t("texto – buscar videos de YouTube", "text – search YouTube videos"),
            f"{c('next')} · {c('back')} – " + t("pasar de página en cualquier lista", "turn pages in any list"),
            f"{c('more')} – " + t("más resultados, la parte siguiente de una página o una respuesta más amplia",
                                   "more results, the next part of a page or a fuller answer"),
            f"{c('summary')} – " + t("respuesta con IA usando los resultados", "AI answer from the results"),
        ]),
        (t("⬇️ Descargas", "⬇️ Downloads"), [
            f"{c('video')} " + t("[baja|media|alta|360] URL o texto", "[low|medium|high|360] URL or text"),
            f"{c('audio')} " + t("[baja|media|alta|64] URL o texto", "[low|medium|high|64] URL or text"),
            f"{c('info')} URL – " + t("calidades y tamaños antes de bajar", "qualities and sizes before downloading"),
            f"{c('transcript')} URL – " + t("subtítulos de un video como texto (pocos KB)", "a video's subtitles as text (a few KB)"),
            f"{c('download')} URL – " + t("cualquier archivo, en partes", "any file, in parts"),
            f"{c('enlace')} URL – " + t("guardar enlace para bajar después", "save a link to download later"),
            f"{c('enlaces')} – " + t("ver enlaces guardados", "see saved links"),
            f"{c('procesar_enlace')} – " + t("procesar el siguiente enlace guardado", "process the next saved link"),
            f"{c('estimar')} URL – " + t("MB estimados y partes antes de bajar", "estimated MB and parts before downloading"),
            f"{c('calidad')} audio64|v360|v480|max50|ahorro – " + t("preset de calidad de descarga", "download quality preset"),
            "",
            t("Con texto en vez de enlace, se busca en YouTube.", "With text instead of a link, YouTube is searched."),
            t("Los videos y audios largos llegan en partes que se reproducen solas.", "Long videos and audio arrive in parts that play on their own."),
            t(f"Otros archivos: abre la parte .001 con 7-Zip o ZArchiver, o únelas en {c('app')}.",
              f"Other files: open the .001 part with 7-Zip or ZArchiver, or join them in {c('app')}."),
        ]),
        (t("📥 Cola y envío", "📥 Queue and delivery"), [
            f"{c('queue')} – " + t("trabajos y acciones", "jobs and actions") + f" · {c('queue', 'N')} · {c('queue', 'clear')}",
            f"{c('pause', 'N')} · {c('resume', 'N')} · {c('cancel', 'N')}",
            f"{c('retry', 'N')} – " + t("repetir uno fallido", "retry a failed job"),
            f"{c('resend', 'N', 'P')} – " + t("reenviar la parte P", "resend part P"),
            f"{c('schedule', 'N', '2230')} – " + t("enviar desde las 22:30", "send from 22:30"),
            f"{c('purge')} – " + t("borrar envíos atascados del buzón de salida", "drop stuck messages from the outbox"),
            "",
            t("Útiles: ", "Useful: ") + " · ".join(c("set", setting_name(locale, key), value) for key, value in
                                                  (("window", "2200-0600"), ("daily_cap_mb", "50"), ("batch", "3"), ("part_mb", "10"))),
        ]),
        (t("🤖 IA e imágenes", "🤖 AI and images"), [
            f"{c('ai')} " + t("texto – forzar respuesta con IA", "text – force an AI answer"),
            f"{c('image')} " + t("texto – generar una imagen con la fuente seleccionada", "text – generate an image with the selected source"),
            f"{c('forget')} – " + t("empezar una conversación nueva", "start a new conversation"),
            f"{c('model')} – " + t("modelos, cuotas y saldo de imágenes", "models, quotas and image balance"),
            f"{c('model', 'auto')} – " + t("rotación automática", "automatic rotation") + f" · {c('model')}_" + t("NOMBRE", "NAME"),
            "",
            t("Envía una nota de voz, foto o documento (con o sin pregunta) y lo transcribo o analizo.",
              "Send a voice note, photo or document (with or without a question) to transcribe or analyze it."),
            t("Enlace + «resume» o «traduce» → resumen o traducción de la página.",
              "Link + «summarize» or «translate» → page summary or translation."),
        ]),
        (t("🧰 Utilidades", "🧰 Tools"), [
            f"{c('translate')} " + t("[idioma] texto – traductor sin IA", "[language] text – translator without AI"),
            f"{c('convert')} " + t("10 km a mi – unidades y temperaturas", "10 km to mi – units and temperatures"),
            f"{c('time')} " + t("ciudad – hora local", "city – local time"),
            f"{c('weather')} " + t("ciudad – pronóstico", "city – forecast"),
            f"{c('rate')} – " + t("elTOQUE: tasas de referencia", "elTOQUE: reference rates") + f" · {c('rate')} EUR · {c('rate')} 100 EUR",
            t("BTC y USDT (TRC20): por USD representado; para calcular usa USD_BTC o USD_USDT_TRC20.",
              "BTC and USDT (TRC20): per represented USD; use USD_BTC or USD_USDT_TRC20 to calculate."),
            t("Conversiones: cálculo de Michiflin, no una nueva cotización de elTOQUE.",
              "Conversions: Michiflin calculations, not new elTOQUE quotes."),
            f"{c('calc')} 2+2 – " + t("calculadora", "calculator"),
            f"{c('news')} " + t("tema – titulares", "topic – headlines"),
            f"{c('nota')} texto – " + t("guardar una nota personal", "save a personal note"),
            f"{c('notas')} – " + t("ver todas tus notas", "see all your notes"),
            f"{c('borrar_nota')} N – " + t("borrar la nota número N", "delete note number N"),
        ]),
        (t("⚙️ Ajustes y estado", "⚙️ Settings and status"), [
            f"{c('settings')} – " + t("todos los ajustes (toca y edita el valor)", "all settings (tap and edit the value)"),
            f"{c('health')} – " + t("estado, próximo reinicio, disco y cuotas", "status, next restart, disk and quotas"),
            f"{c('set', setting_name(locale, 'capture_quality'), 'max' if locale == 'en' else 'maxima')} – "
            + t("capturas aún más nítidas", "even sharper screenshots"),
            f"{c('set', setting_name(locale, 'reactions'), t('si', 'yes'))} – ⏳ ✅ ❌",
            f"{c('set', setting_name(locale, 'language'), 'auto')} – auto · es · en",
            f"{c('github')} · {c('github', 'account')} · {c('github', 'notifications')}",
            f"{c('nauta_strict')} – " + t("partes ≤ 10 MB para Nauta/Cuba", "parts ≤ 10 MB for Nauta/Cuba"),
            f"{c('kill_switch')} – " + t("bloquear/desbloquear descargas", "block/unblock downloads"),
            f"{c('estado')} – " + t("estado corto de tu sesión", "short status of your session"),
        ]),
    ]
    if owner:
        sections.append((t("🛡️ Administración", "🛡️ Administration"), [
            f"{c('admin')} – " + t("usuarios, intentos ajenos y estado", "users, foreign attempts and status"),
            f"{c('owner_only')} – " + t("control privado de esta sesión", "session private-access control"),
            f"{c('maintenance')} " + t("sí | no – pausa global segura", "yes | no – safe global pause"),
            f"{c('admin', 'allow')} " + t("correo – dar acceso", "address – grant access"),
            f"{c('admin', 'remove')} " + t("correo – quitar acceso", "address – revoke access"),
            f"{c('admin', 'block')} " + t("correo – bloquear", "address – block"),
            f"{c('admin', 'attempts')} – " + t("contactos ajenos", "foreign contacts"),
            f"{c('admin', 'logs')} – " + t("advertencias y errores recientes", "recent warnings and errors"),
            f"{c('admin', 'alerts')}_{t('si', 'yes')} · {c('admin', 'alerts')}_no",
            f"{c('admin', 'restart')} – " + t("reiniciar el servidor", "restart the server"),
        ]))
    index = [
        t("Escríbeme normal o envía un enlace; yo decido qué hacer.", "Write normally or send a link; I decide what to do."),
        t("Ej.: «qué es la fotosíntesis», «videos de recetas cubanas», «clima en Holguín». Para dibujos usa /img.",
          "E.g.: «what is photosynthesis», «videos of Cuban recipes», «weather in Havana». For pictures use /img."),
        "",
        f"{c('app')} – " + t("abre la miniapp con todo a mano", "open the mini app with everything at hand"),
        t("Los recibos de lectura dependen de Delta Chat y la red; leer no significa terminar una tarea.",
          "Read receipts depend on Delta Chat and the network; reading does not mean a task is complete."),
        "",
        t("Secciones:", "Sections:"),
    ]
    index += [f"{c('help', number)} {title}" for number, (title, _lines) in enumerate(sections, start=2)]
    index += ["", t(f"Tocar un /comando lo copia en la caja de texto. Un comando solo (ej. {c('capture')}) espera el dato en tu siguiente mensaje.",
                    f"Tapping a /command fills the input box. A bare command (e.g. {c('capture')}) waits for the value in your next message.")]
    pages = [t("😺 Michiflin AI · Ayuda", "😺 Michiflin AI · Help") + "\n\n" + "\n".join(index)]
    pages += [f"{title}\n\n" + "\n".join(lines) for title, lines in sections]
    return pages


def help_pager(locale: str, page: int = 1, owner: bool = False) -> pager.Pager:
    pages = help_pages(locale, owner)
    return pager.Pager("❓ " + tr(locale, "Ayuda", "Help"), pages, locale, "help", index=max(0, min(page, len(pages)) - 1))


@command("help", "start", "h", "menu", "comandos", "commands")
def cmd_help(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    value = (args[0] if args else rest).strip()
    page = int(value) if value.isdigit() else 1
    chat.text(chat_id, pager.show(chat_id, help_pager(locale, page, admin.is_admin(chat.sender()))))


@command("next", "sig", "adelante")
def cmd_next(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, pager.move(chat_id, 1, locale))


@command("back", "prev", "ant", "atras")
def cmd_previous(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, pager.move(chat_id, -1, locale))


@command("app", "miniapp", "aplicacion")
def cmd_app(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    from . import miniapp

    miniapp.send_app(chat_id, locale)


# ---------------------------------------------------------------------------
# Web.
# ---------------------------------------------------------------------------
@command("web", "v", "leer", "read", "vista", "preview", "abrir", "open")
def cmd_web(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "web", locale, "🌐 Envía el enlace a abrir.", "🌐 Send the link to open.")
    web.read(chat_id, url, locale)


@command("more", "más")
def cmd_more(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    if args:
        url = links.resolve(args[0])
        page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 2
        if url:
            web.read(chat_id, url, locale, page)
            return
    chat.text(chat_id, agent.more(chat_id, locale))


@command("pdf")
def cmd_pdf(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "pdf", locale, "📄 Envía el enlace para el PDF.", "📄 Send the link for the PDF.")
    web.pdf(chat_id, url, locale)


@command("capture", "cap", "capturar")
def cmd_capture(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "capture", locale, "📸 Envía el enlace a capturar.", "📸 Send the link to capture.")
    web.capture(chat_id, url, locale)


@command("search", "s", "b")
def cmd_search(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    query = _text(args, rest)
    if not query:
        return _ask(chat_id, "search", locale, "🔎 ¿Qué busco?", "🔎 What should I search?")
    chat.text(chat_id, agent._search_reply(chat_id, query, locale))


@command("videos", "yt", "youtube", "bv")
def cmd_videos(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    query = _text(args, rest)
    if not query:
        return _ask(chat_id, "videos", locale, "🎬 ¿Qué videos busco?", "🎬 Which videos should I search?")
    chat.text(chat_id, agent.video_reply(chat_id, query, locale))


@command("summary", "resumir", "summarize")
def cmd_summary(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, agent.summary(chat_id, locale))


@command("info", "calidades", "qualities")
def cmd_info(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "info", locale, "⚙️ Envía el enlace del video.", "⚙️ Send the video link.")
    web.route(chat_id, url, locale)


# ---------------------------------------------------------------------------
# Downloads.
# ---------------------------------------------------------------------------
def _media_command(chat_id: int, kind: str, args: list[str], rest: str, locale: str) -> None:
    words = rest.split()
    quality_word = words[0] if words and not words[0].startswith("http") else ""
    default = int(state.setting("audio_kbps" if kind == "audio" else "video_height"))
    url = _target(args, rest)
    tail = [item for item in args if not links.resolve(item)]
    quality = media.resolve_quality(kind, quality_word or (tail[0] if tail else ""), default)
    if url:
        web.enqueue_media(chat_id, url, kind, quality, locale)
        return
    if words and media.resolve_quality(kind, words[0], 0):
        words = words[1:]
    query = " ".join(words)
    if query:
        level = media.resolve_quality(kind, quality_word, 0) or media.resolve_quality(kind, tail[0] if tail else "", 0)
        chat.text(chat_id, agent.video_reply(chat_id, query, locale, kind, level))
        return
    mark = "🎧" if kind == "audio" else "🎬"
    _ask(chat_id, kind, locale, f"{mark} Envía el enlace o qué buscar.", f"{mark} Send the link or what to search.")


@command("video", "vid")
def cmd_video(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    _media_command(chat_id, "video", args, rest, locale)


@command("audio", "a", "mp3", "musica", "música", "music")
def cmd_audio(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    _media_command(chat_id, "audio", args, rest, locale)


@command("download", "dl", "descargar", "d")
def cmd_download(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "download", locale, "⬇️ Envía el enlace del archivo.", "⬇️ Send the file link.")
    web.enqueue_download(chat_id, url, locale)


@command("transcript", "subtitles", "subs", "transcripcion")
def cmd_transcript(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "transcript", locale, "📝 Envía el enlace del video.", "📝 Send the video link.")
    if not media.available():
        chat.text(chat_id, tr(locale, "⚠️ Las descargas de video no están disponibles.", "⚠️ Video downloads are unavailable."))
        return
    try:
        title, text = extras.transcript(url, locale if locale in {"es", "en"} else "es")
    except ValueError as exc:
        if str(exc) == "no_subtitles":
            chat.text(chat_id, tr(locale, "📝 Ese video no tiene subtítulos.", "📝 That video has no subtitles."))
        else:
            chat.text(chat_id, media.describe_error(str(exc), locale))
        return
    if len(text) <= 3500:
        chat.text(chat_id, f"📝 {title}\n\n{text}" if title else f"📝 {text}")
        return
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    stem = queue.short_stem(title or tr(locale, "subtitulos", "transcript"))
    path = config.TEMP_ROOT / f"{stem}.txt"
    path.write_text(f"{title}\n{url}\n\n{text}\n", encoding="utf-8")
    chat.send(chat_id, f"📝 {title} · {human_size(path.stat().st_size)}", file=path, filename=path.name)


@command("queue", "q")
def cmd_queue(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    action = names.word_key(args[0] if args else rest)
    if action in {"clear", "vaciar", "empty"}:
        chat.text(chat_id, queue.clear_finished(chat_id, locale))
        return
    if action.isdigit():
        job = queue.get(action)
        chat.text(chat_id, queue.describe_job(job, locale, True) if job else tr(locale, "⚠️ No existe.", "⚠️ Not found."))
        return
    chat.text(chat_id, queue.list_text(chat_id, locale))


def _job_id(args: list[str], rest: str) -> str:
    return (args[0] if args else rest.split()[0] if rest else "").lstrip("#")


@command("pause")
def cmd_pause(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, queue.hold(_job_id(args, rest), True, locale))


@command("resume", "reanudar")
def cmd_resume(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, queue.hold(_job_id(args, rest), False, locale))


@command("send")
def cmd_send(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, queue.hold(_job_id(args, rest), False, locale))


@command("cancel")
def cmd_cancel(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, queue.cancel(_job_id(args, rest), locale))


@command("retry")
def cmd_retry(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, queue.retry(_job_id(args, rest), locale))


@command("resend")
def cmd_resend(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    values = args + rest.split()
    if len(values) < 2 or not values[1].isdigit():
        chat.text(chat_id, tr(locale, f"Uso: {C(locale, 'resend', 'ID', 'PARTE')}", f"Usage: {C(locale, 'resend', 'ID', 'PART')}"))
        return
    chat.text(chat_id, queue.resend(values[0], int(values[1]), locale))


@command("schedule")
def cmd_schedule(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    values = args + rest.split()
    if len(values) < 2:
        chat.text(chat_id, tr(locale, "Uso: ", "Usage: ") + C(locale, "schedule", "ID", "2230"))
        return
    when = values[1] if ":" in values[1] else f"{values[1][:-2]}:{values[1][-2:]}"
    chat.text(chat_id, queue.schedule(values[0], when, locale))


# ---------------------------------------------------------------------------
# AI.
# ---------------------------------------------------------------------------
@command("ai", "gpt", "pregunta", "ask")
def cmd_ai(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    text = _text(args, rest)
    if not text:
        return _ask(chat_id, "ai", locale, "🤖 Escribe tu pregunta.", "🤖 Write your question.")
    url = first_url(text)
    if url and len(text.split()) > 1:
        agent.summarize_url(chat_id, url, text.replace(url, "").strip(), locale)
        return
    chat.text(chat_id, agent.ask(chat_id, text, locale))


@command("image", "imagen", "image", "dibuja", "draw")
def cmd_image(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    text = _text(args, rest)
    if not text:
        return _ask(chat_id, "image", locale, "🎨 Describe la imagen.", "🎨 Describe the image.")
    agent.send_image(chat_id, text, locale)


@command("forget", "nuevo", "new")
def cmd_forget(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    agent.forget(chat_id)
    chat.text(chat_id, tr(locale, "🧹 Conversación reiniciada.", "🧹 Conversation reset."))


@command("model", "modelos", "models", "ias")
def cmd_model(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    choice = (args[0] if args else rest).strip().lower()
    keys = ai.provider_keys()
    if choice in {"auto", "rotacion", "rotación", "rotation"}:
        state.set_setting("ai_pin", "")
        chat.text(chat_id, tr(locale, "🔄 Rotación automática equilibrada.", "🔄 Balanced automatic rotation."))
        return
    if choice in keys:
        state.set_setting("ai_pin", choice)
        undo = C(locale, "model", "auto")
        chat.text(chat_id, tr(locale, f"📌 Preferencia: {choice} (con respaldo automático). {undo} para volver.",
                              f"📌 Preferred: {choice} (with automatic fallback). {undo} to undo."))
        return
    pin = state.setting("ai_pin")
    lines = [tr(locale, "🤖 Modelos de hoy (uso/límite gratis)", "🤖 Today's models (usage/free limit)"), ""]
    lines += ai.status_lines(locale)
    image_lines = images.status_lines()
    if image_lines:
        lines += ["", tr(locale, "🎨 Imágenes", "🎨 Images")] + image_lines
    mode = "📌 " + pin if pin else tr(locale, "rotación", "rotation")
    lines += ["", tr(locale, f"Modo: {mode}", f"Mode: {mode}"),
              " · ".join([C(locale, "model", "auto")] + [C(locale, "model", key) for key in keys])]
    chat.text(chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# Tools without AI.
# ---------------------------------------------------------------------------
@command("translate", "traduce", "traductor", "translator", "tr")
def cmd_translate(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    text = " ".join(args + ([rest] if rest else []))
    if not text:
        return _ask(chat_id, "translate", locale, "🌐 Escribe el texto (opcional: idioma primero, ej. «inglés hola»).",
                    "🌐 Write the text (optional: language first, e.g. «spanish hello»).")
    chat.text(chat_id, extras.translate_text(text, locale))


@command("convert", "conv", "unidades", "units")
def cmd_convert(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, extras.convert_text(" ".join(args + ([rest] if rest else [])), locale))


@command("time", "reloj", "clock")
def cmd_time(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    place = _text(args, rest)
    if not place:
        return _ask(chat_id, "time", locale, "🕒 ¿De qué ciudad?", "🕒 Which city?")
    chat.text(chat_id, extras.time_text(place, locale))


@command("weather", "tiempo", "pronostico")
def cmd_weather(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    city = _text(args, rest)
    if not city:
        return _ask(chat_id, "weather", locale, "🌦️ ¿De qué ciudad?", "🌦️ Which city?")
    chat.text(chat_id, tools.weather_text(city, locale))


@command("rate", "tasas", "rates", "dolar", "dólar")
def cmd_rate(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, tools.rate_text(" ".join(args + rest.split()), locale))


@command("news", "n")
def cmd_news(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, tools.news_text(_text(args, rest), locale))


@command("calc", "calcular", "calculate", "c")
def cmd_calc(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, tools.calc_text(_text(args, rest), locale))


@command("github", "gh", owner=True)
def cmd_github(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    chat.text(chat_id, tools.github_text(" ".join(args + ([rest] if rest else [])), locale))


# ---------------------------------------------------------------------------
# Settings and status.
# ---------------------------------------------------------------------------
def _size0(size: int) -> str:
    return human_size(size) if size else "0 B"


@command("settings", "ajustar", "set", "config")
def cmd_settings(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    values = [item.lower() for item in args + rest.split()]
    key = setting_key(values[0]) if values else ""
    if key and not state.can_change(key):
        chat.text(chat_id, tr(locale, "⚠️ Solo el administrador puede cambiar este ajuste.", "⚠️ Only the administrator can change this setting."))
        return
    if key and len(values) >= 2:
        parser, options = SETTINGS[key][2], SETTINGS[key][4 if locale == "en" else 3]
        parsed = parser("_".join(values[1:]) if key == "window" else values[1])
        if parsed is None or not state.set_setting(key, parsed):
            chat.text(chat_id, tr(locale, f"⚠️ Valor no válido. Opciones: {options}", f"⚠️ Invalid value. Options: {options}"))
            return
        chat.text(chat_id, f"✅ {setting_name(locale, key)} = {display(key, locale)}")
        return
    if key:
        options = SETTINGS[key][4 if locale == "en" else 3]
        chat.text(chat_id, f"⚙️ {setting_name(locale, key)} = {display(key, locale)}\n{tr(locale, 'Opciones', 'Options')}: {options}\n"
                           f"{C(locale, 'set', setting_name(locale, key))}_…")
        return
    lines = [tr(locale, "⚙️ Ajustes (toca y edita el valor)", "⚙️ Settings (tap and edit the value)")]
    for (title_es, title_en), keys in GROUPS:
        keys = [item for item in keys if state.can_change(item)]
        if not keys:
            continue
        lines.append(f"\n{tr(locale, title_es, title_en)}")
        for item in keys:
            shown = display(item, locale).replace(":", "")
            lines.append(f"{C(locale, 'set', setting_name(locale, item), shown)} ({SETTINGS[item][4 if locale == 'en' else 3]})")
    chat.text(chat_id, "\n".join(lines))


@command("health")
def cmd_health(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    elapsed = time.monotonic() - config.STARTED_AT
    remaining = max(0, config.SESSION_SECONDS - elapsed)
    disk = shutil.disk_usage(config.JOB_ROOT if config.JOB_ROOT.exists() else "/")
    version = getattr(getattr(media.yt_dlp, "version", None), "__version__", "—") if media.available() else "—"
    tools_line = " · ".join(f"{'●' if ok else '○'} {name}" for name, ok in (
        ("ffmpeg", bool(shutil.which("ffmpeg"))), ("7z", bool(shutil.which("7z") or shutil.which("7zz"))),
        ("chromium", browser.available()), ("deno", bool(shutil.which("deno"))), ("proxy", bool(config.MEDIA_PROXY))))
    cap = int(state.setting("daily_cap_mb"))
    queue_size, sent = queue.queue_bytes(), usage.sent_today(chat.sender()) if chat.sender() else state.sent_last_day()
    at = local_time(time.time() + remaining, "%H:%M")
    lines = [
        f"🩺 Michiflin AI {__version__}",
        tr(locale, f"⏱️ Activo {span_text(elapsed)} · reinicio en ≈{span_text(remaining)} ({at})",
           f"⏱️ Up {span_text(elapsed)} · restart in ≈{span_text(remaining)} ({at})"),
        tr(locale, f"📥 Cola: {queue.active_count()} activas · {_size0(queue_size)} · disco libre {human_size(disk.free)}",
           f"📥 Queue: {queue.active_count()} active · {_size0(queue_size)} · free disk {human_size(disk.free)}"),
        tr(locale, f"📤 Enviado 24 h: {_size0(sent)}", f"📤 Sent 24 h: {_size0(sent)}") + (f" / {cap} MB" if cap else ""),
        tr(locale, "🤖 IA: ", "🤖 AI: ") + ai.short_status(),
        f"🎨 {len(images.sources())} " + tr(locale, "fuentes de imagen", "image sources"),
        tr(locale, "🔎 Búsqueda: ", "🔎 Search: ") + (f"You.com {state.usage_today('you:search')}/{config.YDC_DAILY} + DuckDuckGo" if config.YDC_API_KEY else "DuckDuckGo"),
        f"🧰 {tools_line} · yt-dlp {version}",
        "",
        " · ".join(C(locale, key) for key in ("model", "queue", "settings")),
    ]
    chat.text(chat_id, "\n".join(lines))


@command("purge", "purgar", owner=True)
def cmd_purge(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    private = access.preserve_history()
    removed = chat.purge_outbox(chat_id=chat_id if private else None)
    chat.text(chat_id, tr(locale, f"🧹 {removed} mensajes pendientes eliminados de " + ("este chat." if private else "la bandeja de salida."),
                          f"🧹 {removed} pending messages removed from " + ("this chat." if private else "the outbox.")))


# ---------------------------------------------------------------------------
# Administration (admin addresses only).
# ---------------------------------------------------------------------------
_owner_challenges: dict[int, tuple[str, float, int, str]] = {}


@command("maintenance", owner=True)
def cmd_maintenance(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    """Emergency text control works even if the owner's mini app is unavailable."""
    if chat.sender().strip().lower() != config.OWNER_ADDRESS:
        chat.text(chat_id, tr(locale, "Solo el dueño puede cambiar mantenimiento.", "Only the owner can change maintenance."))
        return
    from . import maintenance, miniapp
    import threading

    words = args + rest.split()
    current = maintenance.get()
    value = words[0].lower() if words else ""
    if value not in YES | NO:
        chat.text(chat_id, tr(locale,
            f"🛠️ Mantenimiento {'activo' if current['active'] else 'inactivo'}. Uso: /mantenimiento sí | no",
            f"🛠️ Maintenance {'on' if current['active'] else 'off'}. Usage: /maintenance yes | no"))
        return
    active = value in YES
    if active != current["active"]:
        current = maintenance.set_notice(active, current["es"], current["en"])
        def announce() -> None:
            maintenance.sync_profile()
            miniapp._notify_maintenance(current)
        threading.Thread(target=announce, name="maintenance-command-notice", daemon=True).start()
    chat.text(chat_id, tr(locale,
        "🛠️ Mantenimiento activado. Las tareas y colas están pausadas." if active else
        "▶️ Mantenimiento desactivado. Las colas autorizadas pueden continuar; se conservan las pausas manuales y los permisos.",
        "🛠️ Maintenance enabled. Work and queues are paused." if active else
        "▶️ Maintenance disabled. Authorized queues may continue; manual pauses and permissions remain unchanged."))


@command("owner_only", owner=True)
def cmd_owner_only(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    if chat.sender().strip().lower() != config.OWNER_ADDRESS:
        chat.text(chat_id, tr(locale, "Solo el dueño puede cambiar el acceso.", "Only the owner can change access."))
        return
    words = args + rest.split()
    mode = access.snapshot()
    if not words:
        status = "1" if mode["owner_only"] else "0"
        chat.text(chat_id, tr(locale, f"OWNER_ONLY={status}. El cambio dura esta sesión. "
                              "Para cerrar: /solo_dueno 1. Para abrir: /solo_dueno 0.",
                              f"OWNER_ONLY={status}. Changes last this session. "
                              "To close: /owner_only 1. To open: /owner_only 0."))
        return
    value = words[0].lower()
    if value in YES:
        _owner_challenges.pop(chat_id, None)
        access.set_owner_only(True)
        queue.wake()
        chat.text(chat_id, tr(locale, "🔒 OWNER_ONLY=1. Solo entra el dueño.", "🔒 OWNER_ONLY=1. Only the owner may enter."))
        return
    if value not in NO or mode["maintenance"]:
        chat.text(chat_id, tr(locale, "Modo inválido o mantenimiento activo.", "Invalid mode or maintenance is active."))
        return
    pending = _owner_challenges.get(chat_id)
    if pending and len(words) > 1 and secrets.compare_digest(words[1], pending[0]):
        _owner_challenges.pop(chat_id, None)
        if time.time() > pending[1] or not access.set_owner_only(False, pending[2], pending[3]):
            chat.text(chat_id, tr(locale, "La confirmación caducó; solicita otra.", "Confirmation expired; request another."))
            return
        queue.wake()
        duration = tr(locale, "hasta reiniciar" if pending[2] == 0 else f"{pending[2]} min",
                      "until restart" if pending[2] == 0 else f"{pending[2]} min")
        chat.text(chat_id, tr(locale, f"🔓 OWNER_ONLY=0 ({duration}). Nadie obtiene acceso automáticamente. Autoriza cada dirección con /admin_permitir correo.",
                              f"🔓 OWNER_ONLY=0 ({duration}). Nobody gains access automatically. Authorize each address with /admin_allow address."))
        return
    if len(words) > 1 and not words[1].isdigit():
        chat.text(chat_id, tr(locale, "Código incorrecto o caducado.", "Wrong or expired code."))
        return
    minutes = int(words[1]) if len(words) > 1 else 60
    if minutes not in access.DURATIONS:
        chat.text(chat_id, tr(locale, "Duraciones: 15, 60, 240, 1440 minutos o 0 hasta reiniciar.",
                              "Durations: 15, 60, 240, 1440 minutes or 0 until restart."))
        return
    code = secrets.token_hex(3)
    _owner_challenges[chat_id] = (code, time.time() + 300, minutes, mode["nonce"])
    command = C(locale, "owner_only")
    duration_es = f"{minutes} min" if minutes else "hasta reiniciar"
    duration_en = f"{minutes} min" if minutes else "until restart"
    chat.text(chat_id, tr(locale, f"Para confirmar ({duration_es}), responde: {command} 0 {code}\nSolo entrarán las direcciones que autorices; los accesos antiguos siguen cerrados.",
                          f"To confirm ({duration_en}), reply: {command} 0 {code}\nOnly addresses you approve can enter; old grants remain closed."))


@command("admin", owner=True)
def cmd_admin(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    action = names.word_key(args[0]) if args else ""
    extra = args[1:] + rest.split()
    address = next((item.lower() for item in extra if "@" in item), "")
    if action in {"allow", "remove", "block"}:
        if not address:
            chat.text(chat_id, tr(locale, f"Uso: {C(locale, 'admin', action)} correo@dominio", f"Usage: {C(locale, 'admin', action)} name@domain"))
            return
        done = admin.grant(address) if action == "allow" else admin.revoke(address, block=action == "block")
        replies = {
            "allow": (f"✅ {address} puede usar el bot.", f"✅ {address} can use the bot."),
            "remove": (f"✅ {address} ya no tiene acceso.", f"✅ {address} no longer has access."),
            "block": (f"⛔ {address} bloqueado.", f"⛔ {address} blocked."),
        }
        chat.text(chat_id, tr(locale, *replies[action]) if done else tr(locale, "⚠️ No se puede cambiar esa dirección.", "⚠️ That address can't be changed."))
        return
    if action == "logs":
        chat.text(chat_id, admin.recent_logs(locale))
        return
    if action == "alerts":
        value = (extra[0] if extra else "").lower()
        if value not in YES | NO:
            chat.text(chat_id, tr(locale, "Uso: ", "Usage: ") + f"{C(locale, 'admin', 'alerts')}_{tr(locale, 'si', 'yes')} · {C(locale, 'admin', 'alerts')}_no")
            return
        admin.set_alerts(value in YES)
        chat.text(chat_id, tr(locale, f"🔔 Alertas: {'sí' if value in YES else 'no'}", f"🔔 Alerts: {'yes' if value in YES else 'no'}"))
        return
    if action == "restart":
        chat.text(chat_id, admin.restart(locale))
        return
    actions = " · ".join(C(locale, "admin", word) for word in ("allow", "remove", "block", "logs", "restart"))
    chat.text(chat_id, admin.overview(locale) + "\n\n" + actions)


# ---------------------------------------------------------------------------
# B1: Notes / reminders
# ---------------------------------------------------------------------------
@command("nota", "note")
def cmd_nota(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    text = _text(args, rest)
    if not text:
        return _ask(chat_id, "nota", locale, "📝 Escribe la nota.", "📝 Write the note.")
    note_id = state.add_note(chat.sender(), text)
    chat.text(chat_id, tr(locale, f"📝 Nota guardada (#{note_id}).", f"📝 Note saved (#{note_id})."))


@command("notas", "notes")
def cmd_notas(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    notes = state.list_notes(chat.sender())
    if not notes:
        chat.text(chat_id, tr(locale, "📭 No tienes notas guardadas.", "📭 You have no notes saved."))
        return
    lines = [tr(locale, "📝 Tus notas:", "📝 Your notes:")]
    for note in notes:
        lines.append(f"#{note['id']} · {note['text'][:120]}")
    lines.extend(("", f"{C(locale, 'borrar_nota')} N — {tr(locale, 'borrar nota N', 'delete note N')}"))
    chat.text(chat_id, "\n".join(lines))


@command("borrar_nota", "delete_note", "del_note")
def cmd_borrar_nota(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    value = (args[0] if args else rest.split()[0] if rest else "").lstrip("#")
    if not value.isdigit():
        chat.text(chat_id, tr(locale, f"Uso: {C(locale, 'borrar_nota')} N", f"Usage: {C(locale, 'borrar_nota')} N"))
        return
    ok = state.delete_note(chat.sender(), int(value))
    if ok:
        chat.text(chat_id, tr(locale, f"🗑️ Nota #{value} borrada.", f"🗑️ Note #{value} deleted."))
    else:
        chat.text(chat_id, tr(locale, f"⚠️ No existe la nota #{value}.", f"⚠️ Note #{value} not found."))


# ---------------------------------------------------------------------------
# B2: Read-later link queue
# ---------------------------------------------------------------------------
@command("enlace", "link", "save_link")
def cmd_enlace(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = first_url(rest) or (args[0] if args and is_url(args[0]) else "")
    if not url:
        return _ask(chat_id, "enlace", locale, "🔗 Envía el enlace a guardar.", "🔗 Send the link to save.")
    title = rest.replace(url, "").strip()[:100] if rest else ""
    link_id = state.add_link(chat.sender(), url, title)
    chat.text(chat_id, tr(locale, f"🔗 Enlace guardado (#{link_id}).", f"🔗 Link saved (#{link_id})."))


@command("enlaces", "links")
def cmd_enlaces(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    links_q = state.list_links(chat.sender())
    if not links_q:
        chat.text(chat_id, tr(locale, "📭 No tienes enlaces pendientes.", "📭 You have no pending links."))
        return
    lines = [tr(locale, "🔗 Tus enlaces pendientes:", "🔗 Your pending links:")]
    for item in links_q:
        lines.append(f"#{item['id']} · {item['title'] or item['url'][:100]}")
    lines.extend(("", f"{C(locale, 'procesar_enlace')} — {tr(locale, 'procesar el siguiente', 'process next')}"))
    chat.text(chat_id, "\n".join(lines))


@command("procesar_enlace", "process_link", "next_link")
def cmd_procesar_enlace(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    item = state.next_link(chat.sender())
    if not item:
        chat.text(chat_id, tr(locale, "📭 No hay enlaces pendientes.", "📭 No pending links."))
        return
    chat.text(chat_id, tr(locale, f"🔗 Procesando: {item['url']}", f"🔗 Processing: {item['url']}"))
    web.read(chat_id, item["url"], locale)


# ---------------------------------------------------------------------------
# A1: Modo Nauta estricto
# ---------------------------------------------------------------------------
@command("nauta_strict", "nauta", "modo_nauta")
def cmd_nauta_strict(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    value = rest.strip().lower()
    if value in YES:
        state.set_nauta_strict(chat.sender(), True)
        chat.text(chat_id, tr(locale, "📶 Modo Nauta estricto ACTIVADO: partes ≤ 10 MB (o menos según el perfil del buzón).",
                              "📶 Nauta strict mode ON: parts ≤ 10 MB (or less according to the mailbox profile)."))
    elif value in NO:
        state.set_nauta_strict(chat.sender(), False)
        chat.text(chat_id, tr(locale, "📶 Modo Nauta estricto DESACTIVADO.", "📶 Nauta strict mode OFF."))
    else:
        active = state.get_nauta_strict(chat.sender())
        chat.text(chat_id, tr(locale, f"📶 Modo Nauta estricto: {'ACTIVO' if active else 'INACTIVO'}.\nUso: /nauta_strict sí | no",
                              f"📶 Nauta strict mode: {'ON' if active else 'OFF'}.\nUsage: /nauta_strict yes | no"))


# ---------------------------------------------------------------------------
# F2: Kill switch
# ---------------------------------------------------------------------------
@command("kill_switch", "pausar_todo", "pause_all")
def cmd_kill_switch(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    value = rest.strip().lower()
    if value in YES:
        state.set_kill_switch(chat.sender(), True)
        queue.wake()
        chat.text(chat_id, tr(locale, "🛑 Kill switch ACTIVADO: cola, IA y descargas en pausa. Usa /kill_switch no para reanudar.",
                              "🛑 Kill switch ON: queue, AI and downloads paused. Use /kill_switch no to resume."))
    elif value in NO:
        state.set_kill_switch(chat.sender(), False)
        queue.wake()
        chat.text(chat_id, tr(locale, "▶️ Kill switch DESACTIVADO: la cola puede continuar. Revisa Descargas antes de repetir.",
                              "▶️ Kill switch OFF: the queue can continue. Check Downloads before retrying."))
    else:
        active = state.get_kill_switch(chat.sender())
        chat.text(chat_id, tr(locale, f"🛑 Kill switch: {'ACTIVO' if active else 'INACTIVO'}.\nUso: /kill_switch sí | no",
                              f"🛑 Kill switch: {'ON' if active else 'OFF'}.\nUsage: /kill_switch yes | no"))


# ---------------------------------------------------------------------------
# F1: One-line health summary
# ---------------------------------------------------------------------------
@command("estado", "status", "ping")
def cmd_estado(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    address = chat.sender()
    active = queue.active_count()
    qbytes = queue.queue_bytes()
    killed = state.get_kill_switch(address)
    nauta = state.get_nauta_strict(address)
    notes = len(state.list_notes(address))
    links_q = len(state.list_links(address))
    ai_state = ai.short_status() if hasattr(ai, "short_status") else "IA disponible"

    size = (" · " + human_size(qbytes)) if qbytes else ""
    parts_es = ["🟢 Sesión OK" if not killed else "🛑 Kill switch ACTIVO", f"cola {active} activa{size}",
                f"notas {notes} · enlaces {links_q}", f"Nauta {'estricto' if nauta else 'normal'}", ai_state]
    parts_en = ["🟢 Session OK" if not killed else "🛑 Kill switch ON", f"queue {active} active{size}",
                f"notes {notes} · links {links_q}", f"Nauta {'strict' if nauta else 'normal'}", ai_state]
    chat.text(chat_id, " · ".join(parts_en if locale == "en" else parts_es))


# ---------------------------------------------------------------------------
# A2: Data estimate before downloading
# ---------------------------------------------------------------------------
@command("estimar", "estimate")
def cmd_estimar(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    url = _target(args, rest)
    if not url:
        return _ask(chat_id, "estimar", locale, "🔗 Envía el enlace a estimar.", "🔗 Send the link to estimate.")
    if not media.available():
        chat.text(chat_id, tr(locale, "⚠️ Las descargas de video no están disponibles.", "⚠️ Video downloads are unavailable."))
        return
    try:
        info = media.inspect(url)
    except Exception as exc:
        chat.text(chat_id, media.describe_error(str(exc), locale))
        return
    address = chat.sender().strip().lower()
    from . import mail_profiles

    part_mb = mail_profiles.effective_settings(address, {"part_mb": int(state.setting("part_mb"))})["part_mb"]
    if state.get_nauta_strict(address):
        part_mb = min(part_mb, 10)
    lines = [tr(locale, "📊 Estimador de descarga", "📊 Download estimator"), ""]
    lines.append(f"🎬 {info.title[:120]}")
    if info.duration:
        lines.append(f"⏱️ {duration_text(info.duration)}")
    lines.append("")
    if info.video_sizes:
        lines.append(tr(locale, "Video:", "Video:"))
        for height, size in sorted(info.video_sizes.items()):
            parts = queue.parts_for(size, part_mb)
            lines.append(f"  {height}p ≈{human_size(size)} ({parts} {tr(locale, 'partes', 'parts')})")
    if info.audio_sizes:
        lines.append(tr(locale, "Audio:", "Audio:"))
        for rate in (48, 64, 96, 128, 160):
            size = info.audio_sizes.get(rate, 0)
            if size:
                parts = queue.parts_for(size, part_mb)
                lines.append(f"  {rate} kbps ≈{human_size(size)} ({parts} {tr(locale, 'partes', 'parts')})")
    limit_note = ""
    if state.get_nauta_strict(address):
        limit_note = tr(locale, " · Nauta estricto: partes ≤ 10 MB", " · Nauta strict: parts ≤ 10 MB")
    lines.append("")
    lines.append(tr(locale, f"Tamaño de parte: {part_mb} MB{limit_note}", f"Part size: {part_mb} MB{limit_note}"))
    lines.append(tr(locale, "Usa /video o /audio con la calidad deseada para descargar.", "Use /video or /audio with the desired quality to download."))
    chat.text(chat_id, "\n".join(lines))


@command("calidad", "quality", "preset")
def cmd_calidad(chat_id: int, args: list[str], rest: str, locale: str) -> None:
    """D1: named quality presets for downloads (audio64, v360, max50)."""
    from . import state
    key = (rest or " ".join(args) or "").strip().lower().replace(" ", "")
    presets = {
        "audio64": {"video_height": 360, "audio_kbps": 64, "label_es": "Solo audio ~64 kbps", "label_en": "Audio only ~64 kbps"},
        "audio": {"video_height": 360, "audio_kbps": 64, "label_es": "Solo audio ~64 kbps", "label_en": "Audio only ~64 kbps"},
        "v360": {"video_height": 360, "audio_kbps": 64, "label_es": "Vídeo 360p", "label_en": "Video 360p"},
        "360": {"video_height": 360, "audio_kbps": 64, "label_es": "Vídeo 360p", "label_en": "Video 360p"},
        "v480": {"video_height": 480, "audio_kbps": 96, "label_es": "Vídeo 480p", "label_en": "Video 480p"},
        "max50": {"video_height": 360, "audio_kbps": 64, "label_es": "Tope práctico ~50 MB (360p)", "label_en": "Practical ~50 MB cap (360p)"},
        "ahorro": {"video_height": 240, "audio_kbps": 48, "label_es": "Ahorro de datos (240p / 48k)", "label_en": "Data saver (240p / 48k)"},
    }
    if not key:
        lines = [tr(locale, "🎚️ Presets de calidad:", "🎚️ Quality presets:"),
                 "audio64 · v360 · v480 · max50 · ahorro",
                 tr(locale, "Ejemplo: /calidad v360", "Example: /quality v360")]
        h = state.setting("video_height")
        a = state.setting("audio_kbps")
        lines.append(tr(locale, f"Actual: vídeo {h}p · audio {a} kbps", f"Current: video {h}p · audio {a} kbps"))
        chat.text(chat_id, "\n".join(lines))
        return
    preset = presets.get(key)
    if not preset:
        chat.text(chat_id, tr(locale, "Preset desconocido. Usa: audio64, v360, v480, max50, ahorro.",
                              "Unknown preset. Use: audio64, v360, v480, max50, ahorro."))
        return
    state.set_setting("video_height", preset["video_height"])
    state.set_setting("audio_kbps", preset["audio_kbps"])
    label = preset["label_es"] if locale == "es" else preset["label_en"]
    chat.text(chat_id, tr(locale, f"✅ Calidad: {label} ({preset['video_height']}p / {preset['audio_kbps']} kbps).",
                          f"✅ Quality: {label} ({preset['video_height']}p / {preset['audio_kbps']} kbps)."))
