"""Bounded, per-instance delivery of public book files without conversion.

These bytes are temporary RAM only, never the bot's durable state. The WebXDC
bridge carries small independently replayable parts. Access is checked again by
the normal dispatcher on *every* request; a token alone is not authorization.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
import secrets
import threading
import time
import zipfile
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from . import book_library as books

PART_BYTES = 900_000  # base64 plus response envelope remains below the 3 MB replay-cache item limit
MAX_TOTAL = 32_000_000
MAX_SESSIONS = 16
TTL_SECONDS = 1800
_fetch_slots = threading.BoundedSemaphore(4)
_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def _scope(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise books.BookError('transfer_expired')


def _validate(raw: bytes, content_type: str, format: str) -> None:
    if not raw or len(raw) > books.MAX_DOCUMENT:
        raise books.BookError('book_too_large')
    looks_html = ('html' in content_type or raw.lstrip().lower().startswith(
        (b'<!doctype html', b'<html', b'<head', b'<body')))
    if looks_html:
        page = BeautifulSoup(raw[:100_000], 'html.parser')
        password = any(str(field.get('type', '')).lower() == 'password' for field in page.find_all('input'))
        words = page.get_text(' ', strip=True)[:2000].casefold()
        if password or any(word in words for word in ('sign in to read', 'log in to read', 'inicia sesión para leer')):
            raise books.BookError('source_auth_required')
        raise books.BookError('not_a_book_' + format)
    if format == 'txt':
        if b'\0' in raw[:4096] or 'application/json' in content_type:
            raise books.BookError('not_a_book_text')
        return
    if format == 'pdf':
        if b'%PDF-' not in raw[:1024]:
            raise books.BookError('not_a_book_pdf')
        # No attempt is made to decrypt, execute actions or reconstruct pages.
        # A password-protected or malformed file may be saved and exported;
        # the local renderer reports accurately if it cannot be opened.
        return
    if format != 'epub':
        raise books.BookError('format')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 3000 or sum(item.file_size for item in infos) > 24_000_000:
                raise books.BookError('book_too_large')
            names = [item.filename for item in infos]
            if len(set(names)) != len(names):
                raise books.BookError('epub_structure')
            if any(name.startswith('/') or '\\' in name or '\0' in name or '..' in name.split('/') for name in names):
                raise books.BookError('epub_structure')
            container_info = archive.getinfo('META-INF/container.xml')
            if container_info.file_size > 1_000_000:
                raise books.BookError('book_too_large')
            # A publicly downloadable encrypted EPUB may still be preserved and
            # exported as-is. Do not decrypt or try to inspect encrypted entries.
            # encryption.xml can also mean only font obfuscation, not DRM.
            if container_info.flag_bits & 1:
                return
            container = ET.fromstring(archive.read(container_info))
            package = next((node.get('full-path') for node in container.iter()
                            if node.tag.rpartition('}')[2] == 'rootfile'), None)
            if not package or package.startswith('/') or '\\' in package or '..' in package.split('/'):
                raise books.BookError('epub_structure')
            package_info = archive.getinfo(package)
            if package_info.file_size > 1_000_000:
                raise books.BookError('book_too_large')
            if package_info.flag_bits & 1:
                return
            root = ET.fromstring(archive.read(package_info))
            manifest = {node.get('id'): node.get('href') for node in root.iter()
                        if node.tag.rpartition('}')[2] == 'item'}
            spine = [manifest.get(node.get('idref')) for node in root.iter()
                     if node.tag.rpartition('}')[2] == 'itemref']
            if not spine:
                raise books.BookError('epub_structure')
            prefix = package.rpartition('/')[0]
            for name in spine:
                if not isinstance(name, str):
                    raise books.BookError('epub_structure')
                parsed = urlsplit(name)
                path = unquote(parsed.path)
                if (not path or parsed.scheme or parsed.netloc or path.startswith('/') or '\\' in path
                        or '\0' in path or '..' in path.split('/')):
                    raise books.BookError('epub_structure')
                archive.getinfo(prefix + '/' + path if prefix else path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, DefusedXmlException, UnicodeError, RuntimeError, OSError):
        raise books.BookError('epub_structure') from None


def _purge(now: float) -> None:
    for token, record in list(_sessions.items()):
        if now - record['at'] > TTL_SECONDS:
            del _sessions[token]


def _part(record: dict, index: int) -> dict:
    size = len(record['raw'])
    count = (size + PART_BYTES - 1) // PART_BYTES
    if type(index) is not int or not 0 <= index < count:
        raise books.BookError('transfer_expired')
    data = record['raw'][index * PART_BYTES:(index + 1) * PART_BYTES]
    return {'index': index, 'count': count, 'data': base64.b64encode(data).decode('ascii')}


def start(scope: str, url: str, format: str) -> dict:
    """Download exactly the chosen publicly available file; never try another format."""
    _scope(scope)
    if format not in {'txt', 'epub', 'pdf'}:
        raise books.BookError('format')
    if not _fetch_slots.acquire(blocking=False):
        raise books.BookError('source_temporarily_unavailable')
    try:
        raw, content_type = books._fetch(url, books.MAX_DOCUMENT, True, anonymous=True)
        _validate(raw, content_type, format)
    finally:
        _fetch_slots.release()
    now = time.monotonic()
    record = {'scope': scope, 'raw': raw, 'at': now}
    with _lock:
        _purge(now)
        if len(_sessions) >= MAX_SESSIONS or sum(len(item['raw']) for item in _sessions.values()) + len(raw) > MAX_TOTAL:
            raise books.BookError('source_temporarily_unavailable')
        token = secrets.token_urlsafe(24)
        _sessions[token] = record
    return {'transfer': token, 'format': format, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            **_part(record, 0)}


def part(scope: str, token: str, index: int) -> dict:
    _scope(scope)
    with _lock:
        now = time.monotonic()
        _purge(now)
        record = _sessions.get(token) if isinstance(token, str) and len(token) <= 100 else None
        if not record or not secrets.compare_digest(record['scope'], scope):
            raise books.BookError('transfer_expired')
        record['at'] = now
        return _part(record, index)


def close(scope: str, token: str) -> dict:
    _scope(scope)
    with _lock:
        record = _sessions.get(token) if isinstance(token, str) and len(token) <= 100 else None
        if record and secrets.compare_digest(record['scope'], scope):
            del _sessions[token]
    return {'closed': True}
