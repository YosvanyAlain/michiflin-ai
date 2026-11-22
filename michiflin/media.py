"""Streaming-site media (YouTube, Vimeo, Facebook, ...) via yt-dlp with size estimates."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import config, egress, net

log = logging.getLogger("michiflin")

# yt-dlp costs ~35 MiB of resident memory and ~140 ms of import time, and a bot
# can run for days without a single download, so it is loaded on first real use.
# It stays a module attribute: tests patch michiflin.media.yt_dlp with a stub,
# and _ytdlp() returns whatever is already there instead of overwriting it.
_ytdlp_lock = threading.Lock()
_ytdlp_present: bool | None = None


def __getattr__(name: str) -> Any:
    """Load yt-dlp on first attribute access, so `media.yt_dlp` still works (PEP 562)."""
    if name in ("yt_dlp", "gen_extractor_classes"):
        return globals()[name] if _ytdlp() is not None else None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _ytdlp() -> Any:
    """Import yt-dlp the first time something actually needs it."""
    global yt_dlp, gen_extractor_classes
    if globals().get("yt_dlp") is None:
        with _ytdlp_lock:
            if globals().get("yt_dlp") is None:
                try:
                    import yt_dlp as module
                    from yt_dlp.extractor import gen_extractor_classes as classes
                except ImportError:  # pragma: no cover
                    return None
                gen_extractor_classes = classes
                yt_dlp = module
    return globals().get("yt_dlp")


def version() -> str:
    """yt-dlp's version string, loading it only if it is installed."""
    module = _ytdlp() if available() else None
    return getattr(getattr(module, "version", None), "__version__", "—")


VIDEO_LEVELS = {"baja": 360, "media": 480, "alta": 720, "low": 360, "medium": 480, "high": 720}
AUDIO_LEVELS = {"baja": 64, "media": 96, "alta": 160, "low": 64, "medium": 96, "high": 160}
VIDEO_HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160)
AUDIO_RATES = (32, 48, 64, 96, 128, 160, 192, 224, 256, 320)
PROXY_FIRST = re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com|instagram\.com|facebook\.com|fb\.watch|tiktok\.com)$")

_extractors: list[Any] | None = None
_extractor_lock = threading.Lock()
_cookie_file: str = ""


@dataclass
class MediaInfo:
    url: str
    title: str
    uploader: str
    duration: int
    extractor: str
    webpage: str
    live: bool = False
    video_sizes: dict[int, int] = field(default_factory=dict)
    audio_sizes: dict[int, int] = field(default_factory=dict)
    max_height: int = 0
    entries: list[tuple[str, str, int]] = field(default_factory=list)  # playlist: (title, url, duration)


def available() -> bool:
    """True when yt-dlp can be used, without paying for the import to find out."""
    global _ytdlp_present
    if globals().get("yt_dlp") is not None:
        return True
    if _ytdlp_present is None:
        import importlib.util
        try:
            _ytdlp_present = importlib.util.find_spec("yt_dlp") is not None
        except (ImportError, ValueError):  # pragma: no cover
            _ytdlp_present = False
    return _ytdlp_present


def _classes() -> list[Any]:
    global _extractors
    _ytdlp()
    with _extractor_lock:
        if _extractors is None:
            _extractors = [item for item in gen_extractor_classes() if item.IE_NAME not in {"generic"}] if gen_extractor_classes else []
    return _extractors


def supported(url: str) -> bool:
    """True when a site-specific extractor (not the generic one) handles the URL."""
    if not _ytdlp():
        return False
    for extractor in _classes():
        try:
            if extractor.suitable(url) and extractor.working():
                return True
        except Exception:
            continue
    return False


def _cookies() -> str:
    global _cookie_file
    if not config.YTDLP_COOKIES:
        return ""
    # A single configured cookie jar is never shared with other contacts.
    try:
        from . import admin, state
        if not admin.is_admin(state.request_sender()):
            return ""
    except Exception:
        return ""
    if _cookie_file and os.path.exists(_cookie_file):
        return _cookie_file
    value = config.YTDLP_COOKIES
    if os.path.isfile(value):
        _cookie_file = value
        return value
    handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=str(config.TEMP_ROOT) if config.TEMP_ROOT.exists() else None)
    handle.write(value.replace("\\n", "\n"))
    handle.close()
    os.chmod(handle.name, 0o600)
    _cookie_file = handle.name
    return handle.name


YOUTUBE = re.compile(r"(^|\.)(youtube\.com|youtu\.be|youtube-nocookie\.com)$", re.I)
# Fallback clients for streams YouTube refuses (HTTP 403 / SABR-only formats).
YOUTUBE_CLIENTS = ("", "android", "web_embedded")
FINAL_ERRORS = {"private", "unavailable", "geo", "unsupported", "too_large", "disk_full", "ffmpeg_missing", "rate_limited"}

def _options(proxy: str = "") -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"deno": {}, "node": {}},
        "noplaylist": True,
        "socket_timeout": 45,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 3,
        "noprogress": True,
        "geo_bypass": False,
    }
    if proxy:
        options["proxy"] = proxy
    cookies = _cookies()
    if cookies:
        options["cookiefile"] = cookies
    return options


def _routes(url: str) -> list[str]:
    """Egress order: proxy first for sites that block datacenter IPs, direct as fallback."""
    proxy = config.MEDIA_PROXY
    if not proxy:
        return [""]
    host = (urlparse(url).hostname or "").lower()
    return [proxy, ""] if PROXY_FIRST.search(host) else ["", proxy]


def _size(item: dict[str, Any], duration: int) -> int:
    size = item.get("filesize") or item.get("filesize_approx")
    if size:
        return int(size)
    rate = item.get("tbr") or ((item.get("vbr") or 0) + (item.get("abr") or 0))
    return int(rate * 1000 / 8 * duration) if rate and duration else 0


def _estimates(info: dict[str, Any]) -> tuple[dict[int, int], dict[int, int], int]:
    duration = int(info.get("duration") or 0)
    formats = [item for item in info.get("formats") or [] if isinstance(item, dict)]
    audio_only = [item for item in formats if item.get("vcodec") == "none" and item.get("acodec") not in {None, "none"}]
    best_audio = min((_size(item, duration) for item in audio_only if (item.get("abr") or 0) <= 100 and _size(item, duration)), default=0)
    videos = [item for item in formats if item.get("vcodec") not in {None, "none"} and item.get("height")]
    max_height = max((int(item["height"]) for item in videos), default=int(info.get("height") or 0))
    video_sizes: dict[int, int] = {}
    for height in VIDEO_HEIGHTS:
        fitting = [item for item in videos if int(item["height"]) <= height]
        if not fitting:
            continue
        top = max(int(item["height"]) for item in fitting)
        if height > max_height and top < height:
            continue
        candidates = [item for item in fitting if int(item["height"]) == top]
        avc = [item for item in candidates if str(item.get("vcodec", "")).startswith("avc")] or candidates
        chosen = min(avc, key=lambda item: _size(item, duration) or float("inf"))
        chosen_size = _size(chosen, duration)
        extra = best_audio if chosen.get("acodec") in {None, "none"} else 0
        video_sizes[top] = chosen_size + extra if chosen_size else 0
    audio_sizes = {rate: int(rate * 1000 / 8 * duration) for rate in AUDIO_RATES} if duration else {}
    return video_sizes, audio_sizes, max_height


def inspect(url: str) -> MediaInfo:
    # yt-dlp can make its own HTTP requests; never let a user-supplied URL
    # point at loopback, private, link-local, or cloud-metadata addresses.
    url = net.validate_public_url(url)
    if not _ytdlp():
        raise ValueError("ytdlp_missing")
    if not supported(url):
        raise ValueError("unsupported")
    # A saved browser state is never exposed to the regular shared process or
    # another user: inspect it in a network-isolated worker for this sender.
    from . import private_media
    storage = private_media.storage_for(url)
    if storage is not None:
        info = private_media.inspect(url, storage)
    else:
        last: Exception | None = None
        routes = _routes(url)
        info = None
        while routes:
            route = routes.pop(0)
            try:
                with egress.EgressProxy(upstream=route, allow_http=True) as gateway:
                    options = _options(gateway.url)
                    options.update({"skip_download": True, "extract_flat": "in_playlist", "noplaylist": "list=" not in url or "watch?v=" in url})
                    with yt_dlp.YoutubeDL(options) as ydl:
                        info = ydl.extract_info(url, download=False)
                break
            except Exception as exc:  # yt-dlp raises many types
                last = exc
                # Neither the upstream URL nor provider errors (which can
                # contain private media URLs or cookies) belong in logs.
                log.info("media inspect via %s failed: %s", "proxy" if route else "direct", error_code(exc))
                if error_code(exc) in {"bot_check", "rate_limited"}:
                    # Do not switch routes or identities to evade a provider challenge.
                    raise ValueError(error_code(exc)) from None
        if info is None:
            raise ValueError(error_code(last))
    if info.get("_type") == "playlist":
        entries = []
        for entry in (info.get("entries") or [])[:30]:
            if isinstance(entry, dict) and (entry.get("url") or entry.get("webpage_url")):
                target = entry.get("webpage_url") or entry.get("url")
                if not str(target).startswith("http"):
                    target = f"https://www.youtube.com/watch?v={entry.get('id')}"
                entries.append((str(entry.get("title") or "?"), str(target), int(entry.get("duration") or 0)))
        return MediaInfo(url, str(info.get("title") or "Playlist"), str(info.get("uploader") or ""), 0,
                         str(info.get("extractor_key") or ""), str(info.get("webpage_url") or url), entries=entries)
    video_sizes, audio_sizes, max_height = _estimates(info)
    return MediaInfo(
        url=url,
        title=str(info.get("title") or "media"),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        duration=int(info.get("duration") or 0),
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        webpage=str(info.get("webpage_url") or url),
        live=bool(info.get("is_live")),
        video_sizes=video_sizes,
        audio_sizes=audio_sizes,
        max_height=max_height,
    )


def error_code(exc: Exception | None) -> str:
    text = str(exc or "").lower()
    if "disk_full" in text or "no space left on device" in text or "errno 28" in text:
        return "disk_full"
    if any(marker in text for marker in ("sign in to confirm", "not a bot", "captcha", "verify you are human",
                                         "verify you're human", "confirm you're human", "anti-bot")):
        return "bot_check"
    # Preserve gateway categories verbatim: proxy_unavailable must never be
    # mistaken for unavailable *content*. No URLs or raw provider text escapes.
    known = {"bot_check", "private", "unavailable", "geo", "unsupported", "too_large", "disk_full",
             "ytdlp_missing", "vault", "proxy_unavailable", "proxy_invalid", "isolation_unavailable",
             "network", "http_forbidden", "rate_limited", "format_unavailable", "ffmpeg_missing", "javascript_missing"}
    if text in known:
        return text
    if any(marker in text for marker in ("connection refused", "proxyerror", "proxy error", "socks5", "socks error")):
        return "proxy_unavailable" if "proxy" in text or "socks" in text else "network"
    if any(marker in text for marker in ("timed out", "timeout", "name resolution", "getaddrinfo", "network is unreachable",
                                         "network unavailable", "network not available", "connection reset", "unable to connect", "failed to resolve", "nodename nor servname", "tls handshake",
                                         "certificate_verify_failed", "temporary failure in name")):
        return "network"
    if "http error 429" in text or "too many requests" in text:
        return "rate_limited"
    if "http error 403" in text or "403 forbidden" in text:
        return "http_forbidden"
    if "ffmpeg" in text and ("not found" in text or "not installed" in text):
        return "ffmpeg_missing"
    if "requested format is not available" in text or "no video formats found" in text:
        return "format_unavailable"
    if "javascript runtime" in text and ("no supported" in text or "not found" in text):
        return "javascript_missing"
    if "private" in text or "login" in text or "sign in" in text:
        return "private"
    if "unavailable" in text or "removed" in text or "not available" in text:
        return "unavailable"
    if "geo" in text or "country" in text:
        return "geo"
    if "unsupported url" in text:
        return "unsupported"
    if "too large" in text or "max_filesize" in text or "file is larger" in text:
        return "too_large"
    return "media_error"


def format_options(kind: str, quality: int, audio_format: str) -> dict[str, Any]:
    if kind == "audio":
        source_cap = max(48, int(quality * 1.4))
        return {
            "format": f"ba[abr<={source_cap}]/ba/b",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": str(quality)}],
        }
    container = audio_format if audio_format in {"mp4", "mkv", "webm"} else "mp4"
    # WebM needs WebM-compatible codecs; do not pretend an incompatible source
    # was converted. yt-dlp/ffmpeg report a real failure when remuxing cannot work.
    source = "bv*[ext=webm]+ba[ext=webm]/b[ext=webm]/bv*+ba/b" if container == "webm" else "bv*+ba/b"
    sort = [f"res:{quality}", "abr:96", "+size"] if container == "webm" else [f"res:{quality}", "vcodec:h264", "acodec:aac", "abr:96", "+size"]
    return {"format": source, "format_sort": sort, "merge_output_format": container,
            "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": container}]}


def download(url: str, folder: Path, kind: str, quality: int, audio_format: str,
             progress: Callable[[int, int], None] | None = None, stop: Callable[[], bool] | None = None) -> Path:
    if stop and stop():
        raise InterruptedError()
    url = net.validate_public_url(url)
    if not _ytdlp():
        raise ValueError("ytdlp_missing")
    if not supported(url):
        raise ValueError("unsupported")
    folder.mkdir(parents=True, exist_ok=True)
    from . import private_media
    storage = private_media.storage_for(url)
    if storage is not None:
        return private_media.download(url, storage, folder, kind, quality, audio_format, progress, stop)

    last_disk_check = [0.0]
    def hook(data: dict[str, Any]) -> None:
        if stop and stop():
            raise yt_dlp.utils.DownloadCancelled()
        if data.get("status") == "downloading":
            now = time.monotonic()
            if now - last_disk_check[0] >= 1:
                last_disk_check[0] = now
                if shutil.disk_usage(folder).free < config.MIN_FREE_DISK_BYTES:
                    raise ValueError("disk_full")
            if progress:
                progress(int(data.get("downloaded_bytes") or 0), int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0))

    last: Exception | None = None
    youtube = bool(YOUTUBE.search(urlparse(url).hostname or ""))
    routes = _routes(url)
    while routes:
        route = routes.pop(0)
        for client in (YOUTUBE_CLIENTS if youtube else ("",)):
            if stop and stop():
                raise InterruptedError()
            # Keep each extractor/client/route in its own resumable directory.
            # Different clients may select different streams with the same extension;
            # combining their partial bytes could silently corrupt the result.
            identity = "\0".join((url, kind, str(quality), audio_format, route, client))
            attempt = folder / (".resume-" + hashlib.sha256(identity.encode()).hexdigest()[:20])
            attempt.mkdir(parents=True, exist_ok=True)
            try:
                gateway_limit = max(1_000_000_000, config.MAX_FILE_BYTES * 3 + 100_000_000)
                with egress.EgressProxy(upstream=route, max_bytes=gateway_limit, allow_http=True) as gateway:
                    options = _options(gateway.url)
                    options.update({
                        "outtmpl": str(attempt / "media.%(ext)s"),
                        "progress_hooks": [hook],
                        "continuedl": True,
                        "max_filesize": config.MAX_FILE_BYTES,
                        "socket_timeout": 90,
                    })
                    if client:
                        options["extractor_args"] = {"youtube": {"player_client": [client]}}
                    if config.MAX_MEDIA_MINUTES:
                        options["match_filter"] = yt_dlp.utils.match_filter_func(f"duration <= {config.MAX_MEDIA_MINUTES * 60}")
                    options.update(format_options(kind, quality, audio_format))
                    with yt_dlp.YoutubeDL(options) as ydl:
                        ydl.download([url])
                if stop and stop():
                    raise InterruptedError()
                produced = [item for item in attempt.glob("media.*") if item.is_file()
                            and re.fullmatch(r"media\.[a-zA-Z0-9]{1,8}", item.name)]
                if produced:
                    source = max(produced, key=lambda item: item.stat().st_size)
                    target = folder / ("media-result-" + os.urandom(8).hex() + source.suffix.lower())
                    # A new name avoids overwriting any previously recovered source.
                    os.replace(source, target)
                    return target
                last = ValueError("no output")
            except (yt_dlp.utils.DownloadCancelled, InterruptedError):
                raise InterruptedError()
            except Exception as exc:
                if stop and stop():
                    raise InterruptedError() from exc
                last = exc
                code = error_code(exc)
                log.info("media download via %s/%s failed: %s", "proxy" if route else "direct", client or "default", code)
                if code in FINAL_ERRORS or code == "bot_check":
                    # Do not try a different route or client after an explicit challenge.
                    raise ValueError(code) from None
    raise ValueError(error_code(last))


def describe_error(code: str, locale: str) -> str:
    from .util import tr

    messages = {
        "bot_check": ("el sitio pidió verificación. Complétala personalmente en un navegador aceptado; hacerlo en otro dispositivo no siempre autoriza al servidor", "the site requested verification. Complete it yourself in an accepted browser; doing so on another device does not always authorize the server"),
        "network": ("falló la conexión, DNS o TLS; esto no demuestra un bloqueo ni un fallo de WARP", "connection, DNS or TLS failed; this does not prove a provider block or a WARP failure"),
        "http_forbidden": ("el servidor rechazó la solicitud con HTTP 403; no demuestra por sí solo que WARP haya fallado", "the server refused the request with HTTP 403; this alone does not prove a WARP failure"),
        "rate_limited": ("el sitio limitó las solicitudes (HTTP 429); espera antes de volver a intentarlo", "the site rate-limited requests (HTTP 429); wait before trying again"),
        "format_unavailable": ("no hay un formato compatible disponible para esta solicitud", "no compatible format is available for this request"),
        "ffmpeg_missing": ("falta FFmpeg para preparar el audio o vídeo", "FFmpeg is missing for audio or video processing"),
        "javascript_missing": ("falta un intérprete JavaScript compatible para el extractor", "a supported JavaScript runtime is missing for the extractor"),
        "private": ("el contenido es privado o requiere iniciar sesión", "the content is private or requires login"),
        "unavailable": ("el contenido no está disponible", "the content is unavailable"),
        "geo": ("el contenido está bloqueado por región", "the content is region-blocked"),
        "unsupported": ("el enlace no es compatible", "the link is not supported"),
        "too_large": ("el archivo supera el máximo permitido o el espacio disponible", "the file exceeds the allowed maximum or available space"),
        "disk_full": ("no hay espacio suficiente para descargar con seguridad", "there is not enough space to download safely"),
        "ytdlp_missing": ("yt-dlp no está instalado", "yt-dlp is not installed"),
        "vault": ("no pude abrir tu sesión cifrada", "could not open your encrypted session"),
        "proxy_unavailable": ("el proxy no está disponible o rechazó la conexión; no se usó salida directa", "the proxy is unavailable or refused the connection; direct access was not used"),
        "proxy_invalid": ("la configuración del proxy no es compatible", "the proxy configuration is unsupported"),
        "isolation_unavailable": ("no está disponible el entorno aislado para usar tu sesión", "the isolated environment for your session is unavailable"),
    }
    pair = messages.get(code, ("no pude obtener el medio", "could not fetch the media"))
    return "⚠️ " + tr(locale, *pair)


def resolve_quality(kind: str, word: str, default: int) -> int:
    word = (word or "").strip().lower().rstrip("pk")
    levels = AUDIO_LEVELS if kind == "audio" else VIDEO_LEVELS
    if word in levels:
        return levels[word]
    if word.isdigit():
        value = int(word)
        allowed = AUDIO_RATES if kind == "audio" else VIDEO_HEIGHTS
        return min(allowed, key=lambda item: abs(item - value))
    return default
