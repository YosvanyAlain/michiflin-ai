"""Direct-link inspection: resolve share pages, probe size/resume support, detect real file type."""
from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass, asdict
from email.message import Message
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests

from . import net
from .config import MIB
from .util import clean_filename, compact, tr

# (signature check, extension, mime, Spanish description, English description)
KINDS: dict[str, tuple[str, str, str]] = {
    "pdf": ("application/pdf", "Documento PDF", "PDF document"),
    "zip": ("application/zip", "Archivo comprimido ZIP", "ZIP archive"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Documento de Word", "Word document"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Hoja de cálculo de Excel", "Excel spreadsheet"),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "Presentación de PowerPoint", "PowerPoint presentation"),
    "odt": ("application/vnd.oasis.opendocument.text", "Documento OpenDocument", "OpenDocument text"),
    "epub": ("application/epub+zip", "Libro electrónico EPUB", "EPUB e-book"),
    "xdc": ("application/zip", "Mini app de Delta Chat (.xdc)", "Delta Chat mini app (.xdc)"),
    "apk": ("application/vnd.android.package-archive", "Aplicación Android (APK)", "Android app (APK)"),
    "xapk": ("application/zip", "Paquete Android XAPK/APKS", "Android XAPK/APKS bundle"),
    "jar": ("application/java-archive", "Archivo Java (JAR)", "Java archive (JAR)"),
    "rar": ("application/vnd.rar", "Archivo comprimido RAR", "RAR archive"),
    "7z": ("application/x-7z-compressed", "Archivo comprimido 7-Zip", "7-Zip archive"),
    "gz": ("application/gzip", "Archivo comprimido GZIP", "GZIP archive"),
    "bz2": ("application/x-bzip2", "Archivo comprimido BZIP2", "BZIP2 archive"),
    "xz": ("application/x-xz", "Archivo comprimido XZ", "XZ archive"),
    "zst": ("application/zstd", "Archivo comprimido Zstandard", "Zstandard archive"),
    "tar": ("application/x-tar", "Archivo TAR", "TAR archive"),
    "exe": ("application/vnd.microsoft.portable-executable", "Programa de Windows (EXE)", "Windows program (EXE)"),
    "msi": ("application/x-msi", "Instalador de Windows (MSI)", "Windows installer (MSI)"),
    "doc": ("application/msword", "Documento de Office antiguo (DOC/XLS/PPT)", "Legacy Office document"),
    "elf": ("application/x-executable", "Ejecutable de Linux (ELF)", "Linux executable (ELF)"),
    "deb": ("application/vnd.debian.binary-package", "Paquete Debian (DEB)", "Debian package (DEB)"),
    "iso": ("application/x-iso9660-image", "Imagen de disco ISO", "ISO disk image"),
    "dmg": ("application/x-apple-diskimage", "Imagen de disco macOS (DMG)", "macOS disk image (DMG)"),
    "png": ("image/png", "Imagen PNG", "PNG image"),
    "jpg": ("image/jpeg", "Imagen JPEG", "JPEG image"),
    "gif": ("image/gif", "Imagen GIF", "GIF image"),
    "webp": ("image/webp", "Imagen WebP", "WebP image"),
    "bmp": ("image/bmp", "Imagen BMP", "BMP image"),
    "ico": ("image/x-icon", "Icono", "Icon"),
    "svg": ("image/svg+xml", "Imagen vectorial SVG", "SVG vector image"),
    "heic": ("image/heic", "Imagen HEIC", "HEIC image"),
    "mp4": ("video/mp4", "Video MP4", "MP4 video"),
    "m4a": ("audio/mp4", "Audio M4A", "M4A audio"),
    "mov": ("video/quicktime", "Video QuickTime (MOV)", "QuickTime video (MOV)"),
    "3gp": ("video/3gpp", "Video 3GP", "3GP video"),
    "mkv": ("video/x-matroska", "Video Matroska (MKV)", "Matroska video (MKV)"),
    "webm": ("video/webm", "Video WebM", "WebM video"),
    "avi": ("video/x-msvideo", "Video AVI", "AVI video"),
    "flv": ("video/x-flv", "Video FLV", "FLV video"),
    "ts": ("video/mp2t", "Video MPEG-TS", "MPEG-TS video"),
    "mp3": ("audio/mpeg", "Audio MP3", "MP3 audio"),
    "ogg": ("audio/ogg", "Audio OGG", "OGG audio"),
    "opus": ("audio/opus", "Audio Opus", "Opus audio"),
    "flac": ("audio/flac", "Audio FLAC", "FLAC audio"),
    "wav": ("audio/wav", "Audio WAV", "WAV audio"),
    "aac": ("audio/aac", "Audio AAC", "AAC audio"),
    "mid": ("audio/midi", "MIDI", "MIDI"),
    "sqlite": ("application/vnd.sqlite3", "Base de datos SQLite", "SQLite database"),
    "rtf": ("application/rtf", "Documento RTF", "RTF document"),
    "ttf": ("font/ttf", "Fuente TrueType", "TrueType font"),
    "otf": ("font/otf", "Fuente OpenType", "OpenType font"),
    "woff2": ("font/woff2", "Fuente WOFF2", "WOFF2 font"),
    "json": ("application/json", "Datos JSON", "JSON data"),
    "xml": ("application/xml", "Documento XML", "XML document"),
    "csv": ("text/csv", "Tabla CSV", "CSV table"),
    "txt": ("text/plain", "Texto", "Text"),
    "html": ("text/html", "Página web", "Web page"),
    "torrent": ("application/x-bittorrent", "Archivo torrent", "Torrent file"),
    "bin": ("application/octet-stream", "Archivo binario", "Binary file"),
}
EXTENSION_ALIASES = {"jpeg": "jpg", "htm": "html", "tgz": "gz", "oga": "ogg", "m4v": "mp4", "midi": "mid", "xls": "doc", "ppt": "doc", "apks": "xapk"}


def _zip_kind(head: bytes, hint: str) -> str:
    if b"[Content_Types].xml" in head or b"word/" in head or b"xl/" in head or b"ppt/" in head:
        for marker, kind in ((b"word/", "docx"), (b"xl/", "xlsx"), (b"ppt/", "pptx")):
            if marker in head:
                return kind
        return hint if hint in {"docx", "xlsx", "pptx"} else "docx"
    if b"mimetypeapplication/epub+zip" in head:
        return "epub"
    if b"mimetypeapplication/vnd.oasis.opendocument" in head:
        return "odt"
    if b"AndroidManifest.xml" in head or b"classes.dex" in head or hint == "apk":
        return "apk"
    if hint in {"xdc", "xapk", "jar", "apk", "docx", "xlsx", "pptx", "epub", "odt"}:
        return hint
    if b"manifest.toml" in head and b"index.html" in head:
        return "xdc"
    if b"META-INF/MANIFEST.MF" in head:
        return "jar"
    return "zip"


def sniff(head: bytes, hint: str = "") -> str:
    """Return a key of KINDS from the first bytes; hint is the name-derived extension."""
    hint = EXTENSION_ALIASES.get(hint, hint)
    if not head:
        return hint if hint in KINDS else "bin"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return _zip_kind(head, hint)
    checks = [
        (b"Rar!\x1a\x07", "rar"), (b"7z\xbc\xaf\x27\x1c", "7z"), (b"\x1f\x8b", "gz"), (b"BZh", "bz2"),
        (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zst"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"),
        (b"GIF8", "gif"), (b"BM", "bmp"), (b"\x00\x00\x01\x00", "ico"), (b"ID3", "mp3"), (b"fLaC", "flac"),
        (b"MThd", "mid"), (b"FLV", "flv"), (b"SQLite format 3", "sqlite"), (b"{\\rtf", "rtf"),
        (b"\x7fELF", "elf"), (b"!<arch>\ndebian", "deb"), (b"d8:announce", "torrent"), (b"wOF2", "woff2"),
        (b"\x00\x01\x00\x00\x00", "ttf"), (b"OTTO", "otf"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "doc"),
        (b"MZ", "exe"),
    ]
    for signature, kind in checks:
        if head.startswith(signature):
            if kind == "doc" and hint == "msi":
                return "msi"
            if kind == "bmp" and hint != "bmp" and len(head) > 14 and head[6:10] != b"\x00\x00\x00\x00":
                continue
            if kind == "ico" and hint != "ico":
                continue
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"RIFF" and head[8:11] == b"AVI":
        return "avi"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"M4A ", b"M4B ", b"M4P "}:
            return "m4a"
        if brand.startswith(b"qt"):
            return "mov"
        if brand.startswith(b"3g"):
            return "3gp"
        if brand in {b"heic", b"heix", b"mif1", b"msf1"}:
            return "heic"
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm" if b"webm" in head[:64] else "mkv"
    if head.startswith(b"OggS"):
        return "opus" if b"OpusHead" in head[:80] else "ogg"
    if head[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "mp3"
    if head[:2] in {b"\xff\xf1", b"\xff\xf9"}:
        return "aac"
    if head[0:1] == b"\x47" and len(head) > 188 and head[188:189] == b"\x47":
        return "ts"
    if len(head) > 262 and head[257:262] == b"ustar":
        return "tar"
    if head.startswith(b"koly") or hint == "dmg":
        return "dmg" if hint == "dmg" else "bin"
    text = head[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<!doctype html", b"<html", b"<head", b"<!--")) or b"<html" in text[:512]:
        return "html"
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in text):
        return "svg"
    if text.startswith(b"<?xml"):
        return "xml"
    if text[:1] in {b"{", b"["} and hint in {"json", ""}:
        return "json"
    try:
        head[:4096].decode("utf-8")
        printable = b"\x00" not in head[:4096]
    except UnicodeDecodeError:
        printable = False
    if printable:
        return hint if hint in {"csv", "txt", "json", "xml", "svg", "srt", "md"} and hint in KINDS else "txt"
    if hint == "iso" or hint in KINDS:
        return hint
    return "bin"


def describe(kind: str, locale: str) -> str:
    entry = KINDS.get(kind, KINDS["bin"])
    return entry[2] if locale == "en" else entry[1]


@dataclass
class FileInfo:
    url: str
    final_url: str
    name: str
    ext: str
    mime: str
    kind: str
    size: int
    resumable: bool
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Share-page resolvers.
# ---------------------------------------------------------------------------
def _drive(url: str) -> str:
    match = re.search(r"/file/d/([\w-]{10,})", url) or re.search(r"[?&]id=([\w-]{10,})", url)
    return f"https://drive.usercontent.google.com/download?id={match.group(1)}&export=download&confirm=t" if match else url


def _dropbox(url: str) -> str:
    url = re.sub(r"([?&])dl=0", r"\1dl=1", url)
    return url if "dl=1" in url or "raw=1" in url else url + ("&" if "?" in url else "?") + "dl=1"


def _mediafire(url: str) -> str:
    response = net.fetch(url, timeout=(15, 30))
    try:
        match = re.search(r'href="(https://download\d*\.mediafire\.com/[^"]+)"', response.text) or re.search(r'"(https://download\d*\.mediafire\.com/[^"]+)"', response.text)
        return match.group(1) if match else url
    finally:
        response.close()


def _github(url: str) -> str:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
    return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/{match.group(3)}" if match else url


RESOLVERS: list[tuple[str, Callable[[str], str], str]] = [
    (r"(^|\.)drive\.google\.com$", _drive, "Google Drive"),
    (r"(^|\.)dropbox\.com$", _dropbox, "Dropbox"),
    (r"(^|\.)mediafire\.com$", _mediafire, "MediaFire"),
    (r"^github\.com$", _github, "GitHub"),
]


def resolve(url: str) -> tuple[str, str]:
    host = (urlparse(url).hostname or "").lower()
    for pattern, function, label in RESOLVERS:
        if re.search(pattern, host):
            try:
                return function(url), label
            except (requests.RequestException, ValueError):
                return url, label
    return url, ""


def _disposition_name(header: str) -> str:
    if not header:
        return ""
    message = Message()
    message["content-disposition"] = header
    name = message.get_filename() or ""
    star = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", header)
    if star:
        name = unquote(star.group(1))
    return name.strip().strip('"')


def probe(url: str) -> FileInfo:
    """Fetch only the first bytes to learn name, exact size, type and resume support."""
    resolved, source = resolve(url)
    response = net.fetch(resolved, stream=True, headers={"Range": "bytes=0-65535", "Accept-Encoding": "identity"}, timeout=(15, 40))
    try:
        if response.status_code >= 400:
            raise ValueError(f"http_{response.status_code}")
        head = b""
        for chunk in response.iter_content(chunk_size=16384):
            head += chunk
            if len(head) >= 65536:
                break
        final_url = response.url
        content_range = response.headers.get("Content-Range", "")
        total = re.search(r"/(\d+)$", content_range)
        if response.status_code == 206 and total:
            size, resumable = int(total.group(1)), True
        else:
            size = int(response.headers.get("Content-Length") or 0) if response.status_code == 200 else 0
            resumable = response.headers.get("Accept-Ranges", "").lower() == "bytes"
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        name = _disposition_name(response.headers.get("Content-Disposition", ""))
    finally:
        response.close()
    if not name:
        path_name = unquote(Path(urlparse(final_url).path).name) or unquote(Path(urlparse(resolved).path).name)
        query = parse_qs(urlparse(final_url).query)
        name = (query.get("filename") or query.get("file") or [path_name])[0]
    name = clean_filename(name, "archivo")
    hint = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    kind = sniff(head, hint)
    if kind == "bin" and content_type:
        guessed = (mimetypes.guess_extension(content_type) or "").lstrip(".")
        kind = EXTENSION_ALIASES.get(guessed, guessed) if guessed in KINDS or EXTENSION_ALIASES.get(guessed) in KINDS else kind
    ext = hint if kind in {"bin", "txt"} and hint else kind
    if kind == "doc" and hint in {"xls", "ppt", "doc", "msi"}:
        ext = hint
    if kind == "xapk" and hint in {"xapk", "apks"}:
        ext = hint
    if kind == "gz" and name.lower().endswith(".tar.gz"):
        ext = "tar.gz"
    stem = name[: -(len(hint) + 1)] if hint and name.lower().endswith("." + hint) else name
    if name.lower().endswith(".tar.gz"):
        stem = name[:-7]
    final_name = clean_filename(f"{stem}.{ext}" if ext else stem, "archivo")
    mime = KINDS.get(kind, KINDS["bin"])[0] if kind not in {"bin", "txt"} else (content_type or KINDS.get(kind, KINDS["bin"])[0])
    return FileInfo(url, final_url, final_name, ext, mime, kind, size, resumable, source)


def card(info: FileInfo, locale: str, part_mb: int) -> list[str]:
    from .util import human_size, unlinked, host_of

    parts = max(1, -(-info.size // (part_mb * MIB))) if info.size else 0
    lines = [
        f"📦 {info.name}",
        f"{describe(info.kind, locale)} · .{info.ext or '?'} · {info.mime}",
        tr(locale, f"Tamaño: {human_size(info.size)}" + (f" ({info.size:,} B)".replace(",", ".") if info.size else " (desconocido)"),
           f"Size: {human_size(info.size)}" + (f" ({info.size:,} B)" if info.size else " (unknown)")),
        tr(locale, f"Origen: {info.source or unlinked(host_of(info.final_url))} · {'reanudable' if info.resumable else 'sin reanudación'}",
           f"Source: {info.source or unlinked(host_of(info.final_url))} · {'resumable' if info.resumable else 'not resumable'}"),
    ]
    if parts > 1:
        lines.append(tr(locale, f"Se enviará en {parts} partes de {part_mb} MB.", f"Will be sent in {parts} parts of {part_mb} MB."))
    elif info.size:
        lines.append(tr(locale, "Se enviará en un solo mensaje.", "Will be sent as a single message."))
    return lines


def download(url: str, target: Path, size: int, resumable: bool, progress: Callable[[int], None] | None = None,
             stop: Callable[[], bool] | None = None, max_bytes: int = 0) -> int:
    """Resumable download with retries; returns the final byte count."""
    target.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    while True:
        if stop and stop():
            raise InterruptedError()
        done = target.stat().st_size if target.exists() else 0
        if size and done >= size:
            return done
        headers = {"Accept-Encoding": "identity"}
        if done and resumable:
            headers["Range"] = f"bytes={done}-"
        elif done:
            target.unlink()
            done = 0
        try:
            response = net.fetch(url, stream=True, headers=headers, timeout=(20, 90))
            try:
                if stop and stop():
                    raise InterruptedError()
                if response.status_code == 416 and done:
                    return done
                if response.status_code >= 400:
                    raise ValueError(f"http_{response.status_code}")
                if done and response.status_code != 206:
                    done = 0
                    mode = "wb"
                else:
                    mode = "ab" if done else "wb"
                last = time.monotonic()
                with target.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if stop and stop():
                            raise InterruptedError()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if max_bytes and done > max_bytes:
                            raise ValueError("too_large")
                        if progress and time.monotonic() - last > 3:
                            progress(done)
                            last = time.monotonic()
            finally:
                response.close()
            if not size or done >= size:
                return done
        except (requests.RequestException, OSError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            attempts += 1
            if attempts > 6:
                raise ValueError("network") from exc
            deadline = time.monotonic() + min(60, 5 * attempts)
            while time.monotonic() < deadline:
                if stop and stop():
                    raise InterruptedError()
                time.sleep(min(1, max(0, deadline - time.monotonic())))


def summary_line(info: FileInfo) -> str:
    return compact(f"{info.name} · {info.kind}", 120)
