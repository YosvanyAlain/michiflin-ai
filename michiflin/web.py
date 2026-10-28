"""URL actions: automatic routing, reader view, printable PDF, full-page capture and media cards."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

import requests

from . import browser, chat, config, files, links, mail_profiles, media, names, net, progress, queue, reader, state
from .util import clean_filename, duration_text, host_of, human_size, tr, unlinked

log = logging.getLogger("michiflin")

# Hosts whose links are almost always media: a plain link opens the media card.
MEDIA_HOSTS = re.compile(
    r"(^|\.)(youtube\.com|youtu\.be|vimeo\.com|facebook\.com|fb\.watch|instagram\.com|tiktok\.com|twitter\.com|x\.com|"
    r"dailymotion\.com|dai\.ly|twitch\.tv|soundcloud\.com|bandcamp\.com|odysee\.com|rumble\.com|bilibili\.com|"
    r"streamable\.com|ok\.ru|vk\.com|reddit\.com/r/.+/comments|music\.youtube\.com|threads\.net|pinterest\.com|mixcloud\.com|archive\.org/details)$"
)


def url_error(exc: Exception, locale: str) -> str:
    code = str(exc)
    messages = {
        "bad_url": ("el enlace no es una dirección web válida", "the link is not a valid web address"),
        "private_url": ("no se permiten direcciones de redes privadas", "private-network addresses are not allowed"),
        "dns": ("no existe ese dominio o no responde", "that domain does not exist or does not answer"),
        "too_large": ("la página es demasiado grande", "the page is too large"),
        "empty_page": ("la página no tiene texto legible", "the page has no readable text"),
        "redirects": ("demasiadas redirecciones", "too many redirects"),
    }
    if code.startswith("http_"):
        return tr(locale, f"⚠️ El sitio respondió con error {code[5:]}.", f"⚠️ The site answered with error {code[5:]}.")
    if isinstance(exc, requests.Timeout):
        return tr(locale, "⚠️ El sitio tardó demasiado en responder.", "⚠️ The site took too long to answer.")
    if isinstance(exc, requests.RequestException):
        return tr(locale, "⚠️ No pude conectar con el sitio.", "⚠️ Could not connect to the site.")
    pair = messages.get(code)
    if pair:
        return "⚠️ " + tr(locale, *pair) + "."
    if "ERR_NAME_NOT_RESOLVED" in code:
        return "⚠️ " + tr(locale, *messages["dns"]) + "."
    if isinstance(exc, TimeoutError) or "Timeout" in code or "ERR_" in code:
        return tr(locale, "⚠️ El sitio no respondió. Puede que bloquee servidores fuera de su país; prueba más tarde.",
                  "⚠️ The site did not answer. It may block servers outside its country; try again later.")
    return tr(locale, f"⚠️ No pude abrir la página ({exc.__class__.__name__}).", f"⚠️ Could not open the page ({exc.__class__.__name__}).")


def _is_media_host(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    target = host + (parsed.path if "reddit.com" in host or "archive.org" in host else "")
    return bool(MEDIA_HOSTS.search(host) or MEDIA_HOSTS.search(target))


def route(chat_id: int, url: str, locale: str) -> None:
    """Plain URL: media card for streaming sites, file card for files, reader view for pages."""
    try:
        net.validate_public_url(url)
    except ValueError as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    if _is_media_host(url) and media.available():
        media_card(chat_id, url, locale)
        return
    progress.phase(f"🔎 Revisando {unlinked(host_of(url))}…", f"🔎 Checking {unlinked(host_of(url))}…")
    try:
        info = files.probe(url)
    except Exception as exc:
        info = None
        log.info("probe failed for %s: %s", url, exc)
    if info is not None and info.kind not in {"html", "xml"} and not (info.kind == "txt" and info.size and info.size < 200_000 and info.mime.startswith("text/html")):
        file_card(chat_id, info, locale)
        return
    read(chat_id, url, locale)


def read(chat_id: int, url: str, locale: str, page: int = 1) -> None:
    cached = reader.cached(url) if page > 1 else None
    progress.phase(f"🌐 Abriendo {unlinked(host_of(url))}…", f"🌐 Opening {unlinked(host_of(url))}…")
    try:
        loaded = cached or reader.load(url)
    except reader.NotHtml:
        try:
            file_card(chat_id, files.probe(url), locale)
        except Exception as exc:
            chat.text(chat_id, url_error(exc, locale))
        return
    except Exception as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    document, summary, _total = reader.build(loaded, page, locale)
    if media.available() and media.supported(loaded.url):
        summary += f"\n🎬 {links.command('vid', loaded.url)} · 🎧 {links.command('a', loaded.url)}"
    chat.send(chat_id, summary, html=document)


def pdf(chat_id: int, url: str, locale: str) -> None:
    try:
        net.validate_public_url(url)
        if not browser.available():
            raise RuntimeError("browser")
        progress.phase(f"📄 Creando PDF de {unlinked(host_of(url))} (espera a que cargue todo)…", f"📄 Making a PDF of {unlinked(host_of(url))} (waiting for full load)…")
        final_url, title, data = browser.pdf(url)
    except Exception as exc:
        chat.text(chat_id, url_error(exc, locale) if not isinstance(exc, RuntimeError) else tr(locale, "⚠️ El navegador no está disponible.", "⚠️ The browser is not available."))
        return
    name = clean_filename(f"{(title or host_of(final_url))[:70]}.pdf", "pagina.pdf")
    target = config.TEMP_ROOT / f"{int(time.time())}-{name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    chat.send(chat_id, f"📄 {title or unlinked(host_of(final_url))}\n{unlinked(host_of(final_url))} · {human_size(len(data))}", file=target, filename=name, viewtype="File")


def capture(chat_id: int, url: str, locale: str) -> None:
    try:
        net.validate_public_url(url)
        if not browser.available():
            raise RuntimeError("browser")
        progress.phase(f"📸 Capturando {unlinked(host_of(url))} (espera a que cargue todo)…", f"📸 Capturing {unlinked(host_of(url))} (waiting for full load)…")
        final_url, title, images, height = browser.capture(url, state.setting("capture_device"), state.setting("capture_quality"))
    except Exception as exc:
        chat.text(chat_id, url_error(exc, locale) if not isinstance(exc, RuntimeError) else tr(locale, "⚠️ El navegador no está disponible.", "⚠️ The browser is not available."))
        return
    host = host_of(final_url)
    total = len(images)
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    for index, data in enumerate(images, 1):
        stem = tr(locale, "captura", "screenshot")
        name = clean_filename(f"{stem}-{host}-{index}.jpg" if total > 1 else f"{stem}-{host}.jpg")
        target = config.TEMP_ROOT / f"{int(time.time())}-{name}"
        target.write_bytes(data)
        caption = f"📸 {title or unlinked(host)}" + (f" · {index}/{total}" if total > 1 else "") if index == 1 else f"{index}/{total}"
        chat.send(chat_id, caption, file=target, filename=name, viewtype="File")


def _effective_part_mb() -> int:
    return mail_profiles.effective_settings(chat.sender(), {"part_mb": int(state.setting("part_mb"))})["part_mb"]


def media_card(chat_id: int, url: str, locale: str) -> None:
    try:
        url = net.validate_public_url(url)
    except ValueError as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    progress.phase("🔎 Analizando el video y sus calidades…", "🔎 Analyzing the video and its qualities…")
    try:
        info = media.inspect(url)
    except Exception as exc:
        chat.text(chat_id, media.describe_error(str(exc), locale) + f"\n{tr(locale, 'Vista web', 'Web view')}: {links.command('v', url)}")
        return
    if info.entries:
        lines = [f"📃 {info.title}", f"{len(info.entries)} " + tr(locale, "elementos", "items"), ""]
        for index, (title, target, duration) in enumerate(info.entries, 1):
            lines.append(f"{index}. {title[:90]}" + (f" ({duration_text(duration)})" if duration else ""))
            lines.append(f"   🎬 {links.command('vid', target)} · 🎧 {links.command('a', target)}")
        chat.text(chat_id, "\n".join(lines))
        return
    if info.live:
        chat.text(chat_id, tr(locale, "🔴 Es una transmisión en vivo; no se puede descargar ahora.", "🔴 It is a live stream; it cannot be downloaded now."))
        return
    default_height = int(state.setting("video_height"))
    default_kbps = int(state.setting("audio_kbps"))
    part_mb = _effective_part_mb()
    lines = [f"🎬 {info.title[:150]}", " · ".join(filter(None, [info.uploader[:60], duration_text(info.duration) if info.duration else "", info.extractor])), ""]
    if info.video_sizes:
        lines.append(tr(locale, "Video:", "Video:"))
        best_default = max((height for height in info.video_sizes if height <= default_height), default=min(info.video_sizes))
        for height, size in sorted(info.video_sizes.items()):
            star = " ⭐" if height == best_default else ""
            parts = queue.parts_for(size, part_mb)
            lines.append(f"  {height}p ≈{human_size(size)}" + (f" ({parts} p.)" if parts > 1 else "") + f" {links.command('vid', info.webpage, str(height))}{star}")
    if info.audio_sizes:
        lines.append(tr(locale, "Audio:", "Audio:"))
        for rate in (48, 64, 96, 128, 160):
            size = info.audio_sizes.get(rate, 0)
            star = " ⭐" if rate == default_kbps else ""
            lines.append(f"  {rate} kbps ≈{human_size(size)} {links.command('a', info.webpage, str(rate))}{star}")
    lines += ["", tr(locale, "Toca una opción y envíala. ⭐ = ajuste actual.", "Tap an option and send it. ⭐ = current setting.")]
    chat.text(chat_id, "\n".join(lines))


def file_card(chat_id: int, info: files.FileInfo, locale: str) -> None:
    lines = files.card(info, locale, _effective_part_mb())
    lines += ["", tr(locale, f"Descargar: {links.command('dl', info.url)}", f"Download: {links.command('dl', info.url)}")]
    chat.text(chat_id, "\n".join(lines))


def enqueue_download(chat_id: int, url: str, locale: str) -> None:
    try:
        net.validate_public_url(url)
    except ValueError as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    if _is_media_host(url) and media.available():
        enqueue_media(chat_id, url, "video", int(state.setting("video_height")), locale)
        return
    try:
        info = files.probe(url)
    except Exception as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    if info.kind == "html":
        if media.available() and media.supported(url):
            enqueue_media(chat_id, url, "video", int(state.setting("video_height")), locale)
            return
        chat.text(chat_id, tr(locale, f"ℹ️ Ese enlace es una página web, no un archivo. Ábrela con {links.command('v', url)} o guárdala con {links.command('pdf', url)}.",
                              f"ℹ️ That link is a web page, not a file. Open it with {links.command('v', url)} or save it with {links.command('pdf', url)}."))
        return
    job, problem = queue.enqueue_file(chat_id, info, locale)
    if problem:
        chat.text(chat_id, problem)
        return
    _ack(chat_id, job, locale)


def enqueue_media(chat_id: int, url: str, kind: str, quality: int, locale: str) -> None:
    try:
        url = net.validate_public_url(url)
    except ValueError as exc:
        chat.text(chat_id, url_error(exc, locale))
        return
    progress.phase("🔎 Analizando el enlace…", "🔎 Analyzing the link…")
    try:
        info = media.inspect(url)
    except Exception as exc:
        chat.text(chat_id, media.describe_error(str(exc), locale))
        return
    if info.entries:
        media_card(chat_id, url, locale)
        return
    if kind == "video" and info.video_sizes and quality not in info.video_sizes:
        fitting = [height for height in info.video_sizes if height <= quality]
        quality = max(fitting) if fitting else min(info.video_sizes)
    job, problem = queue.enqueue_media(chat_id, info, kind, quality, locale)
    if problem:
        chat.text(chat_id, problem)
        return
    _ack(chat_id, job, locale)


def _ack(chat_id: int, job: dict, locale: str) -> None:
    size = int(job.get("size_est") or 0)
    parts = queue.parts_for(size, int(job["part_mb"]))
    quality = f" · {job['quality']}{'p' if job['kind'] == 'video' else ' kbps'}" if job["kind"] != "file" else ""
    lines = [f"📥 #{job['id']} {job.get('name') or job['title'][:100]}",
             f"{job.get('label', '')}{quality} · {'≈' if job['kind'] != 'file' else ''}{human_size(size)}" + (f" · {parts} " + tr(locale, "partes", "parts") if parts > 1 else "")]
    ahead = sum(1 for item in queue.jobs().values() if item["status"] in {"queued", "downloading", "packing"} and int(item["id"]) < int(job["id"]))
    if ahead:
        lines.append(tr(locale, f"{ahead} trabajo(s) antes en la cola.", f"{ahead} job(s) ahead in the queue."))
    if not state.setting("auto_send"):
        lines.append(tr(locale, f"Cuando esté listo: /enviar_{job['id']}", f"When ready: /send_{job['id']}"))
    head = "\n".join(lines[:2])
    lines.append(f"{names.cmd(locale, 'cancel', job['id'])} · {names.cmd(locale, 'queue')}")
    queue.attach_notice(job, chat.text(chat_id, "\n".join(lines)), head)
