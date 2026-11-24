"""Adapt improved catalogues to the unchanged, already delivered WebXDC UI.

The old client sends a single increasing page number and cannot distinguish a
web page from a whole book. Keep bounded per-instance cursors in RAM, retry
failed provider pages only on an explicit new click, and never send HTML or
unverified generic web links as if they were downloadable books. No book text
or user library is stored here. A restart simply requires a fresh search.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from typing import Any

from . import book_library as books

_SESSION_TTL = 600.0
_MAX_SESSIONS = 32
_MAX_SEEN_KEYS = 25_000
_MAX_ALTERNATIVES = 4096
_ACCEPTED = frozenset({'txt', 'epub', 'pdf'})
_RETRYABLE = frozenset({
    'source_not_found', 'network_timeout', 'network_unavailable', 'book_too_large',
    'epub_structure', 'not_a_book_text', 'not_a_book_epub', 'not_a_book_pdf',
    'no_readable_text', 'pdf_unavailable_or_limit', 'pdf_unreadable_pages',
})


class _Session:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.at = time.monotonic()
        self.last_page = 0
        self.next_page: int | None = 1
        self.source_pages: dict[str, int | None] | None = None
        self.seen: set[bytes] = set()
        self.last_result: dict[str, Any] | None = None


_sessions: dict[tuple[str, str], _Session] = {}
_sessions_lock = threading.Lock()
_alternatives: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
_alternatives_lock = threading.Lock()


def _session_key(scope: str, source: str, query: str, language: str, locale: str) -> tuple[str, str]:
    signature = json.dumps([source, query, language, locale], ensure_ascii=False, separators=(',', ':'))
    return scope, hashlib.sha256(signature.encode()).hexdigest()


def _notice(source: str, code: str, locale: str) -> str:
    names = {'gutenberg': 'Project Gutenberg', 'openlibrary': 'Open Library', 'archive': 'Internet Archive',
             'google': 'Google Books', 'standard': 'Standard Ebooks', 'wikisource': 'Wikisource',
             'web': 'Búsqueda web' if locale == 'es' else 'Web search'}
    reasons = {
        'source_rate_limited': ('límite temporal; espera antes de reintentar', 'temporary rate limit; wait before retrying'),
        'source_forbidden': ('acceso rechazado', 'access refused'),
        'source_auth_required': ('requiere autorización', 'authorization required'),
        'network_timeout': ('tiempo de espera agotado', 'wait timed out'),
        'network_unavailable': ('sin conexión', 'connection unavailable'),
        'source_temporarily_unavailable': ('ocupada temporalmente; espera antes de reintentar', 'temporarily busy; wait before retrying'),
        'metadata_incomplete': ('algunas fichas no se pudieron comprobar', 'some records could not be checked'),
        'editions_unchecked': ('algunas ediciones superaron el límite de comprobación', 'some editions exceeded the checking limit'),
        'catalog_stale': ('catálogo en caché desactualizado', 'outdated cached catalogue'),
        'search_empty_or_unavailable': ('sin resultados o sin respuesta', 'no results or no response'),
        'unsupported': ('sin libros en un formato verificable para esta miniapp', 'no books in a format verifiable for this miniapp'),
    }
    reason = reasons.get(code, ('no disponible temporalmente', 'temporarily unavailable'))
    return f"{names.get(source, source)} ({reason[0 if locale == 'es' else 1]})"


def _restart(locale: str) -> dict:
    message = ('Paginación caducada o incompleta: en Ajustes borra solo la caché de biblioteca y busca otra vez; los libros guardados no se borran.'
               if locale == 'es' else
               'Pagination expired or is incomplete: in Settings clear only the library cache and search again; saved books are not deleted.')
    return {'books': [], 'page': 1, 'more': False, 'unavailable': [message], 'source_errors': {}, 'warnings': {}}


def _remember_formats(scope: str, book: dict) -> None:
    formats = []
    for item in book.get('downloads', []):
        if (isinstance(item, dict) and item.get('format') in _ACCEPTED and isinstance(item.get('url'), str)
                and item['url'].startswith('https://')):
            pair = {'url': item['url'], 'format': item['format']}
            if pair not in formats:
                formats.append(pair)
    now = time.monotonic()
    with _alternatives_lock:
        for item in formats:
            key = (scope, item['url'], item['format'])
            choices = [other for other in formats if other != item and books.same_file_family(item['url'], other['url'])][:2]
            previous = _alternatives.get(key)
            if previous and now - previous[0] <= _SESSION_TTL and previous[1] != choices:
                # An identical file URL was attributed to conflicting records.
                # Do not guess which other edition to try.
                choices = []
            _alternatives[key] = (now, choices)
        for key in list(_alternatives):
            if now - _alternatives[key][0] > _SESSION_TTL:
                _alternatives.pop(key, None)
        while len(_alternatives) > _MAX_ALTERNATIVES:
            _alternatives.pop(next(iter(_alternatives)))


def reading_copy(scope: str, url: str, format: str, *, visual: bool = False) -> dict:
    """Retry at most two public formats of this same previously shown record."""
    try:
        return books.reading_copy(url, format, visual=True) if visual else books.reading_copy(url, format)
    except Exception as exc:
        if books.failure_code(exc) not in _RETRYABLE:
            raise
        original = exc
    with _alternatives_lock:
        saved = _alternatives.get((scope, url, format))
        choices = list(saved[1]) if saved and time.monotonic() - saved[0] <= _SESSION_TTL else []
    last = original
    for item in choices:
        try:
            return (books.reading_copy(item['url'], item['format'], visual=True) if visual
                    else books.reading_copy(item['url'], item['format']))
        except Exception as exc:
            last = exc
            # Never try another link around authorization, 403, 429, loans or DRM.
            if books.failure_code(exc) not in _RETRYABLE:
                raise
    raise last


def _supported(book: dict) -> dict | None:
    if not isinstance(book, dict) or not isinstance(book.get('id'), str) or not book['id']:
        return None
    # The old UI cannot say "unverified link" or label a saved HTML page. Do
    # not present either as a confirmed downloadable book on that UI.
    if book.get('verification') == 'candidate' and not book.get('metadata_checked'):
        return None
    downloads = [item for item in book.get('downloads', [])
                 if isinstance(item, dict) and item.get('format') in _ACCEPTED and isinstance(item.get('url'), str)]
    return {**book, 'downloads': downloads} if downloads else None


def search(scope: str, source: str, query: str, page: int = 1, language: str = '', *, locale: str = 'es') -> dict:
    """Translate old-client page clicks into isolated per-provider cursors."""
    if (not re.fullmatch(r'[0-9a-f]{64}', str(scope)) or source not in books.SOURCES
            or not isinstance(query, str) or len(query) > 200 or type(page) is not int or not 1 <= page <= 1000
            or not isinstance(language, str) or language and not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2})?', language)):
        raise books.BookError('invalid_query')
    query, language = query.strip(), language.lower().split('-')[0]
    locale = locale if locale in {'es', 'en'} else 'es'
    key = _session_key(scope, source, query, language, locale)
    now = time.monotonic()
    with _sessions_lock:
        for old_key, old in list(_sessions.items()):
            if not old.active and now - old.at > _SESSION_TTL:
                _sessions.pop(old_key, None)
        session = _sessions.get(key)
        if session is None:
            if page != 1:
                return _restart(locale)
            while len(_sessions) >= _MAX_SESSIONS:
                candidates = [(old.at, old_key) for old_key, old in _sessions.items() if not old.active]
                if not candidates:
                    raise books.BookError('source_temporarily_unavailable')
                _sessions.pop(min(candidates)[1], None)
            session = _sessions[key] = _Session()
        session.active += 1
    try:
        with session.lock:
            if page == 1:
                session.last_page, session.next_page, session.seen = 0, 1, set()
                # Wikisource returns HTML pages only; the old UI cannot label
                # them honestly as partial or save them as complete books.
                session.source_pages = {name: None if name == 'wikisource' else 1 for name in books.COMBINED_SOURCES}
                session.last_result = None
            elif page == session.last_page and session.last_result is not None:
                return copy.deepcopy(session.last_result)  # idempotent duplicate request
            elif page != session.last_page + 1:
                return _restart(locale)  # never guess over a cached/skipped page

            if source == 'wikisource':
                raw = {'books': [], 'page': page, 'more': False, 'unavailable': [source],
                       'source_errors': {source: 'unsupported'}, 'warnings': {}}
            elif source == 'all':
                raw = books.search('all', query, min(page, 100), language, refresh=page == 1,
                                   locale=locale, source_pages=session.source_pages)
                session.source_pages = raw['source_pages']
            elif session.next_page is None:
                raw = {'books': [], 'page': page, 'more': False, 'unavailable': [], 'source_errors': {}, 'warnings': {}}
            else:
                actual = session.next_page
                try:
                    raw = books.bounded_source_search(source, query, actual, language, refresh=page == 1, locale=locale)
                    if raw.get('retry_page') == actual:
                        session.next_page = actual
                    elif raw.get('more') and actual < 100:
                        session.next_page = actual + 1
                    else:
                        session.next_page = None
                except Exception as exc:
                    code = books.failure_code(exc)
                    if code == 'invalid_query':
                        raise
                    raw = {'books': [], 'page': page, 'more': True, 'unavailable': [source],
                           'source_errors': {source: code}, 'warnings': {}}
                    # Keep the exact failed provider page for the next click.
                    session.next_page = actual

            display, limit = [], False
            for original in raw.get('books', []):
                book = _supported(original)
                if not book:
                    continue
                identities = ['row:' + book['id']]
                if isinstance(book.get('archive_id'), str) and book['archive_id']:
                    identities.append('archive:' + book['archive_id'])
                tokens = {hashlib.sha256(value.encode()).digest() for value in identities}
                if session.seen & tokens:
                    continue
                if len(session.seen | tokens) > _MAX_SEEN_KEYS:
                    limit = True
                    break
                session.seen.update(tokens)
                display.append(book)
                _remember_formats(scope, book)

            notices = [_notice(name, raw.get('source_errors', {}).get(name, 'source_unavailable'), locale)
                       for name in raw.get('unavailable', [])]
            for name, codes in raw.get('warnings', {}).items():
                for code in codes:
                    notices.append(_notice(name, code, locale))
            if source not in {'all', 'wikisource'} and raw.get('books') and not display and not session.seen:
                notices.append(_notice(source, 'unsupported', locale))
            if limit:
                notices.append('Límite de resultados alcanzado; concreta la búsqueda.' if locale == 'es'
                               else 'Result limit reached; make the search more specific.')
            result = {**raw, 'books': display, 'page': page, 'more': bool(raw.get('more')) and not limit,
                      'unavailable': list(dict.fromkeys(notices))}
            session.last_page, session.last_result = page, copy.deepcopy(result)
            return result
    finally:
        with _sessions_lock:
            session.active -= 1
            session.at = time.monotonic()
