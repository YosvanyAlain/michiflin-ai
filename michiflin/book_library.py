"""Public book catalogues and bounded, non-DRM reading copies.

Catalogue availability never implies download rights. No login, loan bypass,
API key, user file path or executable document content is accepted here.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
import subprocess
import sys
import os
import tempfile
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from . import book_search, net, tools

SOURCES = {'gutenberg', 'openlibrary', 'archive', 'google', 'standard', 'wikisource', 'web', 'all'}
COMBINED_SOURCES = ('gutenberg', 'openlibrary', 'archive', 'google', 'standard', 'wikisource')
MAX_DOCUMENT = 8_000_000
MAX_TEXT = 3_000_000
WEB_PAGE_SIZE = 12
WEB_RESULT_LIMIT = 48
_cache: dict[str, tuple[float, object]] = {}
_source_pause: dict[str, float] = {}
_source_pause_reason: dict[str, str] = {}
_cache_lock = threading.Lock()
_archive_gate = threading.BoundedSemaphore(4)
_gutenberg_lock = threading.Lock()

# A stalled catalogue must not hold every other provider's results until the
# app operation times out. These bounds are shared across all users, not one
# pool per click. A timed-out request may finish in the background; a later
# explicit click joins the same in-flight work instead of starting it again.
SOURCE_WAIT_SECONDS = 18.0
_source_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix='book-source')
_source_slots = threading.BoundedSemaphore(10)  # no long backlog beyond the worker count
_source_inflight: dict[tuple, Future] = {}
_source_lock = threading.RLock()


class BookError(ValueError):
    pass


def _public_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or any(key in parsed.query.lower() for key in ['api_key=', 'access_token=', 'password='])):
            raise ValueError
    except (TypeError, ValueError):
        raise BookError('public_https_required') from None


def _fetch(url: str, maximum: int = MAX_DOCUMENT, guard_archive: bool = False) -> tuple[bytes, str]:
    # For a reading copy, validate each redirect too. In particular, an
    # arbitrary web link may redirect to an Archive loan's OCR or EPUB file.
    # Do not let that route bypass the same public-item check as catalogue UI.
    checked: dict[tuple[str, str], str] = {}
    current = url
    for _ in range(10):
        _public_url(current)
        if guard_archive:
            current = _guard_archive_url(current, checked)
            _public_url(current)
        options = {'follow_redirects': False} if guard_archive else {}
        response = net.request('GET', current, stream=True, timeout=(10, 20), **options)
        try:
            target = response.headers.get('Location') if guard_archive and response.status_code in {301, 302, 303, 307, 308} else None
            if target:
                current = urljoin(current, target)
                continue
            response.raise_for_status()
            data = net.read_limited(response, maximum)
            content_type = response.headers.get('content-type', '').lower()
            parsed = urlsplit(current)
            if (guard_archive and parsed.hostname == 'standardebooks.org' and not parsed.query
                    and parsed.path.lower().endswith('.epub') and 'html' in content_type):
                # Standard Ebooks publicly serves a "download started" page
                # with an immediate meta refresh to this same file. Follow
                # only that exact documented-on-page link, not arbitrary HTML,
                # authentication flows, challenges or alternative hosts.
                soup = BeautifulSoup(data, 'html.parser')
                title = soup.title.get_text(' ', strip=True) if soup.title else ''
                refresh = soup.find('meta', attrs={'http-equiv': re.compile('^refresh$', re.I)})
                match = re.fullmatch(r'\s*0\s*;\s*url\s*=\s*(\S+)\s*', str(refresh.get('content', '')), re.I) if refresh else None
                target_url = urljoin(current, match[1]) if match and title.startswith('Your Download Has Started') else ''
                target = urlsplit(target_url)
                if (target.scheme == 'https' and target.hostname == parsed.hostname and target.path == parsed.path
                        and target.query == 'source=download' and not target.fragment):
                    current = target_url
                    continue
            return data, content_type
        finally:
            response.close()
    raise BookError('redirects')


def _json(url: str) -> dict:
    data, _ = _fetch(url, 3_000_000)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise BookError('catalogue_format')
    return value


_gutenberg_client = None


def _gutenberg(query: str, page: int, language: str) -> dict:
    global _gutenberg_client
    from pathlib import Path
    from . import config
    from .gutenberg_catalog import GutenbergClient
    if _gutenberg_client is None:
        with _gutenberg_lock:
            if _gutenberg_client is None:
                _gutenberg_client = GutenbergClient(Path(config.TEMP_ROOT) / 'public-book-catalog')
    return _gutenberg_client.get('https://gutendex.com/books/', {'search': query, 'page': page,
        'copyright': 'false', **({'languages': language} if language else {})})


def _row(source: str, identifier: str, title: str, authors: list, url: str, **extra) -> dict:
    return {'id': source + ':' + str(identifier), 'source': source, 'title': str(title)[:300],
            'authors': [str(author)[:160] for author in authors[:8]], 'url': url,
            'cover_url': '', 'downloads': [], 'access': 'catalogue', **extra}


def failure_code(exc: Exception) -> str:
    """Stable, non-sensitive reasons for UI diagnostics and safe retries."""
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', 0) if response is not None else 0
    if status:
        return {401: 'source_auth_required', 403: 'source_forbidden', 404: 'source_not_found',
                429: 'source_rate_limited'}.get(status, 'source_unavailable')
    if isinstance(exc, requests.Timeout):
        return 'network_timeout'
    if isinstance(exc, requests.RequestException):
        return 'network_unavailable'
    code = str(getattr(exc, 'code', '') or (str(exc) if isinstance(exc, BookError) else ''))
    allowed = {'invalid_query', 'source_temporarily_unavailable', 'source_rate_limited', 'source_unavailable',
               'source_auth_required', 'source_forbidden', 'source_not_found', 'source_restricted',
               'catalogue_format', 'source_response_invalid', 'source_response_limit', 'public_https_required',
               'book_too_large', 'protected_book', 'epub_structure', 'pdf_unavailable_or_limit',
               'pdf_unreadable_pages', 'not_a_book_text', 'not_a_book_pdf', 'not_a_book_epub',
               'not_a_book_html', 'no_readable_text', 'unrecognized_format', 'format', 'identifier', 'redirects'}
    if code in allowed:
        return code
    if isinstance(exc, ValueError) and str(exc) == 'too_large':
        return 'book_too_large'
    if isinstance(exc, (zipfile.BadZipFile, KeyError, StopIteration, ET.ParseError, DefusedXmlException)):
        return 'epub_structure'
    if isinstance(exc, ValueError) and str(exc) == 'dns':
        return 'network_unavailable'
    if isinstance(exc, ValueError) and str(exc) in {'private_url', 'bad_url'}:
        return 'public_https_required'
    return 'source_unavailable'


def _paused_reason(source: str) -> str:
    with _cache_lock:
        if time.monotonic() < _source_pause.get(source, 0):
            return _source_pause_reason.get(source, 'source_temporarily_unavailable')
    return ''


def _pause_after_http(source: str, exc: Exception, statuses: frozenset[int] = frozenset({403, 429})) -> None:
    response = getattr(exc, 'response', None)
    if response is None or getattr(response, 'status_code', 0) not in statuses:
        return
    retry = str(response.headers.get('Retry-After', '300')).strip()
    if retry.isdigit():
        wait_seconds = int(retry) if len(retry) <= 10 else float('inf')
    else:
        try:
            until = parsedate_to_datetime(retry)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            wait_seconds = until.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            wait_seconds = 300
    # Preserve long server-requested waits instead of silently reducing them.
    # Implausibly large values remain paused for this process's lifetime.
    delay = max(60, wait_seconds)
    deadline = float('inf') if delay > 366 * 86400 else time.monotonic() + delay
    with _cache_lock:
        if deadline >= _source_pause.get(source, 0):
            _source_pause[source] = deadline
            _source_pause_reason[source] = failure_code(exc)


def _submit_source(source: str, query: str, page: int, language: str, locale: str, refresh: bool) -> Future | None:
    """Bound and coalesce a provider page, including work still finishing after a timeout."""
    key = (source, query, page, language, locale)
    with _source_lock:
        previous = _source_inflight.get(key)
        if previous and not previous.done():
            return previous
        if previous:
            _source_inflight.pop(key, None)
        slots = _source_slots
        if not slots.acquire(blocking=False):
            return None
        try:
            future = _source_pool.submit(search, source, query, page, language, refresh=refresh, locale=locale)
        except BaseException:
            slots.release()
            raise
        _source_inflight[key] = future

        def release(completed: Future) -> None:
            with _source_lock:
                if _source_inflight.get(key) is completed:
                    _source_inflight.pop(key, None)
            slots.release()

        future.add_done_callback(release)
        return future


def bounded_source_search(source: str, query: str, page: int, language: str, *, refresh: bool, locale: str) -> dict:
    """Wait a bounded time for one provider without launching automatic retries."""
    if source not in SOURCES or source == 'all':
        raise BookError('invalid_query')
    future = _submit_source(source, query, page, language, locale, refresh)
    if future is None:
        raise BookError('source_temporarily_unavailable')
    done, _ = wait([future], timeout=SOURCE_WAIT_SECONDS)
    if future not in done:
        raise requests.Timeout('catalogue wait deadline')
    return future.result()


def search(source: str, query: str, page: int = 1, language: str = '', *, refresh: bool = False,
           locale: str = 'es', source_pages: dict | None = None) -> dict:
    if (source not in SOURCES or not isinstance(query, str) or len(query) > 200 or type(page) is not int
            or not 1 <= page <= 100 or not isinstance(language, str)
            or language and not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2})?', language)):
        raise BookError('invalid_query')
    if source_pages is not None:
        if (source != 'all' or type(source_pages) is not dict or set(source_pages) != set(COMBINED_SOURCES)
                or any(value is not None and (type(value) is not int or not 1 <= value <= 100)
                       for value in source_pages.values())):
            raise BookError('invalid_query')
    query, language = query.strip(), language.lower().split('-')[0]
    locale = locale if locale in {'es', 'en'} else 'es'
    cache_key = json.dumps([source, query, page, language, locale, source_pages], sort_keys=True)
    with _cache_lock:
        hit = _cache.get(cache_key)
    if not refresh and hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    if source == 'all':
        books, unavailable, errors, warnings = [], [], {}, {}
        requested = source_pages if source_pages is not None else {name: page for name in COMBINED_SOURCES}
        next_pages: dict[str, int | None] = {name: None for name in COMBINED_SOURCES}
        names = [name for name in COMBINED_SOURCES if requested[name] is not None]
        futures = {name: _submit_source(name, query, requested[name], language, locale, refresh) for name in names}
        done, _ = wait([future for future in futures.values() if future is not None], timeout=SOURCE_WAIT_SECONDS)
        for name in names:
            current_page, future = requested[name], futures[name]
            item, error = None, ''
            if future is None:
                error = 'source_temporarily_unavailable'
            elif future not in done:
                error = 'network_timeout'
            else:
                try:
                    item = future.result()
                except Exception as exc:
                    error = failure_code(exc)
            if item is None:
                unavailable.append(name)
                errors[name] = error
                next_pages[name] = current_page  # retry this source, not its next page
            else:
                # Every provider's complete page is included. Slicing each
                # to six silently lost results 7–20 on all later pages.
                books.extend(item['books'])
                warnings.update(item.get('warnings', {}))
                if 'metadata_incomplete' in item.get('warnings', {}).get(name, []):
                    next_pages[name] = current_page  # recover failed records on another explicit click
                elif item.get('more') and current_page < 100:
                    next_pages[name] = current_page + 1
        books.sort(key=lambda book: -int(book.get('_rank', 0)))
        seen, unique = set(), []
        for book in books:
            # Two catalogues can point to the exact same Archive edition.
            # Deduplicate that edition, not distinct works or provider pages.
            identity = ('archive', book['archive_id']) if book.get('archive_id') else ('row', book.get('id'))
            if identity not in seen:
                seen.add(identity)
                unique.append(book)
        result = {'books': unique, 'page': page, 'more': any(value is not None for value in next_pages.values()),
                  'source_pages': next_pages, 'unavailable': unavailable, 'source_errors': errors, 'warnings': warnings}
    else:
        if reason := _paused_reason(source):
            raise BookError(reason)
        diagnostics: list[str] = []
        try:
            books, more = _search(source, query, page, language, diagnostics, locale)
        except Exception as exc:
            _pause_after_http(source, exc)
            raise
        books = [book for book in books if book.get('downloads')]
        books.sort(key=lambda book: -int(book.get('_rank', 0)))
        partial_page = 'metadata_incomplete' in diagnostics
        result = {'books': books, 'page': page, 'more': bool(partial_page or (more and page < 100)),
                  'retry_page': page if partial_page else None, 'unavailable': [], 'source_errors': {},
                  'warnings': {source: sorted(set(diagnostics))} if diagnostics else {}}
    # An incomplete combined result must not keep a recovered source hidden
    # for ten minutes. Explicit refresh still respects provider rate limits.
    if not result['unavailable'] and not result['warnings']:
        with _cache_lock:
            _cache[cache_key] = (time.monotonic(), result)
            while len(_cache) > 64:
                _cache.pop(next(iter(_cache)))
    return result


def _search(source: str, query: str, page: int, language: str, diagnostics: list[str] | None = None, locale: str = 'es') -> tuple[list, bool]:
    diagnostics = diagnostics if diagnostics is not None else []
    books = []
    if source == 'gutenberg':
        data = _gutenberg(query, page, language)
        if data.get('partial_results'):
            diagnostics.append('metadata_incomplete')
        if data.get('catalog_stale'):
            diagnostics.append('catalog_stale')
        for row in data.get('results', [])[:24]:
            if not isinstance(row, dict) or row.get('copyright') is not False or type(row.get('id')) is not int:
                continue
            downloads = [{'format': 'txt' if mime.startswith('text/plain') else 'epub', 'url': url}
                         for mime, url in row.get('formats', {}).items() if mime.startswith(('text/plain', 'application/epub+zip')) and isinstance(url, str) and url.startswith('https://')]
            authors = [a.get('name', '') for a in row.get('authors', []) if isinstance(a, dict)]
            rank = book_search.relevance(query, row.get('title', ''), authors, row.get('subjects', []))
            rank += book_search.language_bonus(row.get('languages', []), locale)
            books.append(_row(source, str(row['id']), row.get('title', ''), authors,
                              f"https://www.gutenberg.org/ebooks/{row['id']}", downloads=downloads,
                              cover_url=row.get('formats', {}).get('image/jpeg', ''), access='public_domain_us',
                              languages=row.get('languages', []), _rank=max(0, rank)))
        return books, bool(data.get('next'))
    if source == 'openlibrary':
        # Open Library returns works, not a guarantee that the first Archive
        # edition is public. Ask for public works, then check each chosen item.
        key_terms = book_search.primary(query)
        expression = ('(' + ' '.join('"' + word + '"' for word in key_terms) + ')' if key_terms else '(subject:literature)') + ' AND ebook_access:public'
        params = {'q': expression, 'page': page, 'limit': 20, 'lang': language or locale,
                  'fields': 'key,title,author_name,first_publish_year,cover_i,ebook_access,ia,subject,language'}
        data = _json('https://openlibrary.org/search.json?' + urlencode(params))
        candidates = []
        for row in data.get('docs', [])[:20]:
            if not isinstance(row, dict) or row.get('ebook_access') != 'public':
                continue
            key = str(row.get('key', '')).rsplit('/', 1)[-1]
            if not re.fullmatch(r'OL\d+W', key):
                continue
            authors = row.get('author_name') if isinstance(row.get('author_name'), list) else []
            rank = book_search.relevance(query, row.get('title', ''), authors, row.get('subject', []))
            if rank < 0:
                continue
            rank += book_search.language_bonus(row.get('language', []), locale)
            identifiers = row.get('ia') if isinstance(row.get('ia'), list) else []
            identifiers = [str(item) for item in identifiers if re.fullmatch(r'[\w.-]{1,160}', str(item))]
            # Prefer a Gutenberg edition when the work lists one, but still
            # verify its current metadata and every file before offering it.
            identifiers.sort(key=lambda item: not item.lower().endswith('gut'))
            if not identifiers:
                continue
            cover = row.get('cover_i')
            item = _row(source, key, row.get('title', ''), authors, 'https://openlibrary.org/works/' + key,
                        cover_url=f'https://covers.openlibrary.org/b/id/{cover}-M.jpg' if type(cover) is int else '',
                        access='public_metadata_check', year=row.get('first_publish_year'), _rank=rank)
            candidates.append((item, identifiers[:4], len(identifiers) > 4))

        def resolve(candidate):
            item, identifiers, more_editions = candidate
            failed = False
            for identifier in identifiers:
                try:
                    downloads = archive_download(identifier, expected_title=item['title'], expected_authors=item['authors'])
                    if downloads:
                        return {**item, 'archive_id': identifier, 'downloads': downloads, 'title_checked': True}, failed, False
                except Exception as exc:
                    failed = True
                    if failure_code(exc) in {'source_rate_limited', 'source_forbidden', 'source_auth_required'}:
                        break
            return None, failed, more_editions and not failed

        with ThreadPoolExecutor(max_workers=3) as pool:
            for item, failed, unchecked in pool.map(resolve, candidates):
                if item:
                    books.append(item)
                if failed:
                    diagnostics.append('metadata_incomplete')
                if unchecked:
                    diagnostics.append('editions_unchecked')
        return books, page * 20 < int(data.get('numFound', data.get('num_found', 0)) or 0)
    if source == 'google':
        data = _json('https://www.googleapis.com/books/v1/volumes?' + urlencode({'q': query or 'public domain', 'filter': 'free-ebooks', 'maxResults': 20, 'startIndex': (page - 1) * 20}))
        for row in data.get('items', [])[:20]:
            volume, access = row.get('volumeInfo', {}), row.get('accessInfo', {})
            identifier = str(row.get('id', ''))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier):
                continue
            downloads = []
            for format in ['epub', 'pdf']:
                url = access.get(format, {}).get('downloadLink', '')
                if isinstance(url, str) and url.startswith('https://'):
                    downloads.append({'format': format, 'url': url})
            cover = str(volume.get('imageLinks', {}).get('thumbnail', '')).replace('http://', 'https://', 1)
            rank = book_search.relevance(query, volume.get('title', ''), volume.get('authors', []), volume.get('categories', []))
            if rank < 0:
                continue
            rank += book_search.language_bonus(volume.get('language', ''), locale)
            books.append(_row(source, identifier, volume.get('title', ''), volume.get('authors', []),
                              'https://books.google.com/books?id=' + identifier, cover_url=cover,
                              downloads=downloads, access=str(access.get('accessViewStatus', 'catalogue')),
                              description=str(volume.get('description', ''))[:3000], _rank=rank))
        return books, page * 20 < int(data.get('totalItems', 0))
    if source == 'archive':
        # Search relevant metadata, not full text with implicit ORs. The latter
        # returned CIA bulletins for "tutorial de python". Stable popularity
        # ordering gives Archive a deterministic provider page before filtering.
        key_terms = book_search.primary(query) or ['literature']
        clauses = [f'(title:("{word}") OR creator:("{word}") OR subject:("{word}"))' for word in key_terms]
        expression = 'mediatype:texts AND ' + ' AND '.join(clauses)
        params = [('q', expression), ('output', 'json'), ('rows', '20'), ('page', str(page)),
                  ('sort[]', 'downloads desc'), ('sort[]', 'identifier asc'), ('fl[]', 'identifier'), ('fl[]', 'title'),
                  ('fl[]', 'creator'), ('fl[]', 'subject'), ('fl[]', 'language'), ('fl[]', 'licenseurl')]
        data = _json('https://archive.org/advancedsearch.php?' + urlencode(params))
        response = data.get('response', {})
        for row in response.get('docs', [])[:20]:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get('identifier', ''))
            if not re.fullmatch(r'[\w.-]{1,160}', identifier):
                continue
            author = row.get('creator', [])
            author = author if isinstance(author, list) else [author]
            rank = book_search.relevance(query, row.get('title', ''), author, row.get('subject', []))
            if rank < 0:
                continue
            rank += book_search.language_bonus(row.get('language', []), locale)
            books.append(_row(source, identifier, row.get('title', ''), author,
                              'https://archive.org/details/' + quote(identifier),
                              cover_url='https://archive.org/services/img/' + quote(identifier), access='check_provider',
                              archive_id=identifier, _rank=rank))
        # A catalogue id is not evidence of an available download. Resolve
        # bounded public metadata and omit loans/restricted/unsupported records.
        def resolve(book):
            try:
                downloads = archive_download(book['archive_id'])
                return ({**book, 'downloads': downloads} if downloads else None), False
            except Exception:
                return None, True
        with ThreadPoolExecutor(max_workers=3) as pool:
            resolved = list(pool.map(resolve, books))
        if any(failed for _, failed in resolved):
            diagnostics.append('metadata_incomplete')
        return [book for book, _ in resolved if book], page * 20 < int(response.get('numFound', 0) or 0)
    if source == 'standard':
        # The old /opds/all feed redirects to a members-only feed. The public
        # website provides its own search; use it without credentials or
        # attempting to get around the feed restriction.
        params = {'query': query, 'page': page, 'per-page': 12}
        html, _ = _fetch('https://standardebooks.org/ebooks?' + urlencode(params), 3_000_000)
        soup = BeautifulSoup(html, 'html.parser')
        listing = soup.select_one('ol.ebooks-list')
        if listing is None:
            if 'no ebooks matched your filters' in soup.get_text(' ', strip=True).casefold():
                return [], False
            raise BookError('catalogue_format')
        candidates = []
        for node in listing.select(':scope > li')[:12]:
            path = str(node.get('about') or '')
            parts = path.split('/')
            if (len(parts) < 4 or parts[:2] != ['', 'ebooks'] or
                    any(not re.fullmatch(r'[A-Za-z0-9._~-]+', part) or part in {'.', '..'} for part in parts[2:])):
                continue
            title_node = node.select_one('p:not(.author) [property="schema:name"]')
            title = title_node.get_text(' ', strip=True) if title_node else ''
            authors = [author.get_text(' ', strip=True) for author in node.select('p.author [property="schema:name"]')]
            rank = book_search.relevance(query, title, authors)
            if not title or rank < 0:
                continue
            url = 'https://standardebooks.org' + path
            image = node.select_one('img[src]')
            cover = urljoin(url, str(image.get('src', ''))) if image else ''
            if urlsplit(cover).hostname != 'standardebooks.org' or not cover.startswith('https://'):
                cover = ''
            candidates.append(_row(source, hashlib.sha256(url.encode()).hexdigest()[:24], title, authors, url,
                                   cover_url=cover, access='public_domain_us', _rank=rank))

        def resolve(book):
            try:
                details, _ = _fetch(book['url'], 3_000_000)
                page_soup = BeautifulSoup(details, 'html.parser')
                prefix = urlsplit(book['url']).path.rstrip('/') + '/downloads/'
                downloads = []
                for link in page_soup.select('a[href]'):
                    url = urljoin(book['url'], str(link.get('href') or ''))
                    parsed = urlsplit(url)
                    if (parsed.scheme == 'https' and parsed.hostname == 'standardebooks.org' and
                            parsed.path.startswith(prefix) and parsed.path.lower().endswith('.epub') and
                            not parsed.query and not parsed.fragment):
                        downloads.append({'format': 'epub', 'url': url})
                # Prefer the ordinary compatible EPUB, then other public editions.
                downloads.sort(key=lambda item: ('_advanced.epub' in item['url'] or '.kepub.epub' in item['url']))
                return ({**book, 'downloads': downloads[:3]} if downloads else None), False
            except Exception:
                return None, True

        with ThreadPoolExecutor(max_workers=3) as pool:
            resolved = list(pool.map(resolve, candidates))
        if any(failed for _, failed in resolved):
            diagnostics.append('metadata_incomplete')
        nav = soup.select_one('nav.pagination')
        next_link = next((link for link in nav.select('a') if link.get_text(' ', strip=True).casefold() == 'next'), None) if nav else None
        return [book for book, _ in resolved if book], bool(next_link and next_link.get('href') and next_link.get('aria-disabled') != 'true')
    if source == 'wikisource':
        language = 'en' if (language or locale) == 'en' else 'es'
        params = {'action': 'query', 'generator': 'search', 'gsrsearch': query or 'libro', 'gsrnamespace': 0,
                  'gsrlimit': 12, 'gsroffset': (page - 1) * 12, 'prop': 'extracts', 'exintro': 1, 'explaintext': 1, 'format': 'json'}
        data = _json('https://' + language + '.wikisource.org/w/api.php?' + urlencode(params))
        rows = data.get('query', {}).get('pages', {})
        for row in rows.values():
            identifier = row.get('pageid')
            if type(identifier) is not int or identifier < 1:
                continue
            title = str(row.get('title', ''))
            url = 'https://' + language + '.wikisource.org/wiki/' + quote(title.replace(' ', '_'))
            rank = book_search.relevance(query, title, extra=row.get('extract', ''))
            if rank < 0:
                continue
            books.append(_row(source, str(identifier), title, [], url, languages=[language],
                downloads=[{'format': 'html', 'url': url}], access='public_reading_page', reading_scope='page',
                description=str(row.get('extract', ''))[:3000] + '\nPágina de Wikisource; puede contener enlaces a otros capítulos.', _rank=rank))
        return books, bool(data.get('continue'))
    if source == 'web':
        web_language = 'en' if (language or locale) == 'en' else 'es'
        search_query = query + (' free ebook' if web_language == 'en' else ' libro gratis')
        results = tools.search(search_query, web_language, limit=WEB_RESULT_LIMIT + 1)
        if not results:
            if tools.search_limited():
                raise BookError('source_rate_limited')
            # The shared search interface can return [] for either a genuine
            # empty query or transport/provider failure; do not invent which.
            diagnostics.append('search_empty_or_unavailable')
        candidates = []
        for item in results:
            url = str(item.url)
            try:
                _public_url(url)
            except BookError:
                continue
            rank = book_search.relevance(query, item.title, extra=str(item.snippet) + ' ' + urlsplit(url).path)
            if rank < 0:
                continue
            suffix = urlsplit(url).path.rsplit('.', 1)[-1].lower()
            format = suffix if suffix in {'txt', 'epub', 'pdf', 'html', 'htm'} else 'auto'
            candidates.append(_row(source, hashlib.sha256(url.encode()).hexdigest()[:24], item.title, [], url,
                                   downloads=[{'format': format, 'url': url}], description=item.snippet,
                                   access='verify_rights', verification='candidate', _rank=rank))
        candidates.sort(key=lambda book: -book['_rank'])
        total = min(len(candidates), WEB_RESULT_LIMIT)
        candidates = candidates[(page - 1) * WEB_PAGE_SIZE:min(page * WEB_PAGE_SIZE, WEB_RESULT_LIMIT)]

        def resolve(book):
            reference = _archive_reference(book['url'])
            host = (urlsplit(book['url']).hostname or '').lower()
            if not reference:
                # Never turn an unrecognised Archive file route into a way
                # around the loan/DRM metadata check.
                if host == 'archive.org' or host.endswith('.archive.org'):
                    return None, False
                return book, False
            identifier, filename = reference
            try:
                downloads = archive_download(identifier)
                if not downloads and filename:
                    downloads = archive_download(identifier, filename=filename)
                return ({**book, 'archive_id': identifier, 'downloads': downloads, 'metadata_checked': True} if downloads else None), False
            except Exception:
                return None, True

        with ThreadPoolExecutor(max_workers=3) as pool:
            resolved = list(pool.map(resolve, candidates))
        if any(failed for _, failed in resolved):
            diagnostics.append('metadata_incomplete')
        available = [book for book, _ in resolved if book]
        return available, page * WEB_PAGE_SIZE < total
    raise BookError('source')


def _file_family(name: str) -> str:
    """Normalize common derivative suffixes, not proof of bibliographic identity."""
    stem = book_search.fold(name.rsplit('/', 1)[-1])
    while True:
        shorter = re.sub(r'(?:\.(?:txt|epub|pdf)|[-_.](?:djvu|bw|text|ocr|images|noimages|advanced|kepub)(?:[-_]3)?)$', '', stem)
        if shorter == stem:
            break
        stem = shorter
    return re.sub(r'[^\w]+', '', stem)


def same_file_family(left: str, right: str) -> bool:
    """Conservatively recognize two variants in the same public file directory."""
    try:
        a, b = urlsplit(left), urlsplit(right)
        if (a.scheme != 'https' or b.scheme != 'https' or not a.hostname or a.hostname != b.hostname
                or a.port != b.port or a.username is not None or b.username is not None
                or a.password is not None or b.password is not None or a.query or b.query):
            return False
        first, second = unquote(a.path), unquote(b.path)
        if any(part in {'.', '..'} for path in (first, second) for part in path.split('/')):
            return False
        return first.rpartition('/')[0] == second.rpartition('/')[0] and bool(_file_family(first)) and _file_family(first) == _file_family(second)
    except (TypeError, ValueError, UnicodeError):
        return False


def _archive_reference(url: str) -> tuple[str, str] | None:
    """Identify public Archive catalogue/file routes, including official mirrors."""
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    if host != 'archive.org' and not host.endswith('.archive.org'):
        return None
    try:
        path = unquote(parsed.path, errors='strict')
    except (ValueError, UnicodeError):
        return None
    parts = path.lstrip('/').split('/')
    if len(parts) >= 2 and parts[0] in {'details', 'download'}:
        identifier, filename = parts[1], '/'.join(parts[2:]) if parts[0] == 'download' else ''
    elif (re.fullmatch(r'(?:ia|dn)[0-9]+(?:\.[a-z]{2})?\.archive\.org', host) and len(parts) >= 4
          and parts[0].isdigit() and parts[1] == 'items'):
        identifier, filename = parts[2], '/'.join(parts[3:])
    else:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', identifier) or identifier in {'.', '..'}:
        return None
    if filename and (any(part in {'', '.', '..'} for part in filename.split('/')) or '\\' in filename
                     or re.search(r'[\x00-\x1f\x7f]', filename)):
        return None
    return identifier, filename


def _archive_restricted(data: dict) -> bool:
    metadata = data.get('metadata', {})
    truthy = {'1', 'true', 'yes', 'on'}
    if not isinstance(metadata, dict):
        return True
    for key in ('access-restricted-item', 'is_restricted', 'is_dark', 'lending_enabled'):
        if any(str(container.get(key, '')).strip().lower() in truthy for container in (data, metadata)):
            return True
    collections = []
    for key in ('collection', 'collection_set', 'collection_added'):
        value = metadata.get(key, [])
        collections.extend(value if isinstance(value, list) else [value])
    if any(any(flag in str(value).lower() for flag in ('borrow', 'printdisabled', 'inlibrary', 'lending')) for value in collections):
        return True
    identifiers = metadata.get('external-identifier', [])
    identifiers = identifiers if isinstance(identifiers, list) else [identifiers]
    if any(re.search(r'(?i)\burn:(?:lcp|acs\d*):', str(value)) for value in identifiers):
        return True
    return False


def archive_download(identifier: str, *, filename: str | None = None, expected_title: str = '',
                     expected_authors: list | None = None) -> list[dict]:
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', identifier) or identifier in {'.', '..'}:
        raise BookError('identifier')
    with _archive_gate:
        # Open Library, Archive and web candidates share this endpoint. Once
        # it returns 429, stop queued metadata requests as well as refreshes;
        # an item-specific 403 does not imply a provider-wide embargo.
        if reason := _paused_reason('archive'):
            raise BookError(reason)
        try:
            data = _json('https://archive.org/metadata/' + quote(identifier, safe=''))
        except Exception as exc:
            _pause_after_http('archive', exc, frozenset({429}))
            raise
    metadata = data.get('metadata', {})
    # A file list with missing item metadata is not enough to confirm that
    # this is a publicly readable text item. Fail closed on malformed data.
    if (_archive_restricted(data) or metadata.get('mediatype') != 'texts'
            or (metadata.get('identifier') and metadata['identifier'] != identifier)):
        return []
    if expected_title and not book_search.same_edition(expected_title, metadata.get('title'),
                                                       expected_authors or [], metadata.get('creator')):
        return []  # an Open Library work can contain an unrelated IA identifier
    candidates = []
    for file in data.get('files', [])[:1000]:
        if not isinstance(file, dict):
            continue
        name = str(file.get('name', ''))
        private = str(file.get('private', '')).strip().lower() in {'true', '1', 'yes', 'on'}
        if (private or not name or '\\' in name or any(part in {'', '.', '..'} for part in name.split('/'))
                or re.search(r'[\x00-\x1f\x7f]', name) or (filename is None and '/' in name)):
            continue
        if filename is not None and name not in {filename, filename.replace('+', ' ')}:
            continue
        lower = name.lower()
        format = 'txt' if lower.endswith('.txt') else 'epub' if lower.endswith('.epub') else 'pdf' if lower.endswith('.pdf') else ''
        if not format or 'encrypt' in str(file.get('format', '')).lower():
            continue
        if format == 'txt' and re.search(r'(?:^|/)(?:readme|license|licence|changelog|faq)(?:[._ -]|$)', lower):
            continue
        size = str(file.get('size', ''))
        if size.isdigit() and int(size) > MAX_DOCUMENT:
            continue
        candidates.append({'format': format, 'url': 'https://archive.org/download/' + quote(identifier, safe='') + '/' + quote(name, safe='/')})
    if filename is None:
        candidates = [item for item in candidates if _file_family(unquote(urlsplit(item['url']).path))]
        roots = {_file_family(unquote(urlsplit(item['url']).path)) for item in candidates}
        title = book_search.fold(metadata.get('title', ''))
        collection_title = re.search(r'\b(?:ebooks|books|libros|coleccion|collection|recopilacion|compilation)\b', title)
        # A small collection can also contain unrelated books. Only select a
        # family when the item identifier or title identifies exactly one;
        # otherwise don't let the UI pick an arbitrary TXT as this record.
        if len(roots) > 1:
            if collection_title:
                return []
            expected = {_file_family(identifier + '.txt'), _file_family(title + '.txt')}
            matching = roots & expected
            if len(matching) != 1:
                return []
            selected = next(iter(matching))
            candidates = [item for item in candidates if _file_family(unquote(urlsplit(item['url']).path)) == selected]
    # If a malformed search link uses '+' for spaces, only repair it when the
    # provider's actual metadata names exactly one matching public file.
    if filename is not None and len(candidates) > 1:
        exact = [item for item in candidates if unquote(urlsplit(item['url']).path).endswith('/' + filename)]
        if len(exact) == 1:
            candidates = exact
        else:
            return []
    order = {'txt': 0, 'epub': 1, 'pdf': 2}
    return sorted(candidates, key=lambda item: (order[item['format']], item['url']))[:4]


def _guard_archive_url(url: str, checked: dict[tuple[str, str], str]) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    if host != 'archive.org' and not host.endswith('.archive.org'):
        return url
    reference = _archive_reference(url)
    if not reference:
        if re.search(r'(?i)(?:/download/|/items/|\.(?:txt|epub|pdf)(?:$|/))', parsed.path):
            raise BookError('source_restricted')
        return url  # A catalogue page can only be saved as a labelled HTML page.
    identifier, filename = reference
    if not filename:
        return url
    key = (identifier, filename)
    if key not in checked:
        downloads = archive_download(identifier, filename=filename)
        if not downloads:
            raise BookError('source_restricted')
        canonical = downloads[0]['url']
        canonical_ref = _archive_reference(canonical)
        if not canonical_ref:
            raise BookError('source_restricted')
        checked[key] = canonical
        checked[canonical_ref] = canonical
    canonical = checked[key]
    actual = _archive_reference(canonical)
    return canonical if actual and actual[1] != filename else url


def reading_copy(url: str, format: str) -> dict:
    if format not in {'txt', 'epub', 'pdf', 'html', 'htm', 'auto'}:
        raise BookError('format')
    raw, content_type = _fetch(url, MAX_DOCUMENT, True)
    looks_html = ('html' in content_type or raw.lstrip().lower().startswith((b'<!doctype html', b'<html', b'<head', b'<body')))
    html_soup = BeautifulSoup(raw, 'html.parser') if looks_html else None
    if html_soup:
        password_form = any(str(field.get('type', '')).lower() == 'password' for field in html_soup.find_all('input'))
        page_words = html_soup.get_text(' ', strip=True)[:2000].casefold()
        if password_form or any(phrase in page_words for phrase in ('sign in to read', 'log in to read', 'inicia sesión para leer')):
            raise BookError('source_auth_required')
    if format == 'auto':
        if b'%PDF-' in raw[:1024]:
            format = 'pdf'
        elif zipfile.is_zipfile(io.BytesIO(raw)):
            format = 'epub'
        elif looks_html:
            format = 'html'
        elif 'text/plain' in content_type and b'\x00' not in raw[:4096]:
            format = 'txt'
        else:
            raise BookError('unrecognized_format')
    if format == 'txt':
        if looks_html or b'\x00' in raw[:4096] or 'application/json' in content_type:
            raise BookError('not_a_book_text')
        text = raw.decode('utf-8-sig', errors='replace')
    elif format == 'epub':
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise BookError('not_a_book_epub')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > 3000 or sum(item.file_size for item in infos) > 24_000_000:
                    raise BookError('book_too_large')
                if any('encryption.xml' in item.filename.lower() or item.flag_bits & 1 for item in infos):
                    raise BookError('protected_book')
                container = ET.fromstring(archive.read('META-INF/container.xml'))
                package = next((element.get('full-path') for element in container.iter()
                                if element.tag.rpartition('}')[2] == 'rootfile'), None)
                if not package or package.startswith('/') or '..' in package.split('/') or '\\' in package:
                    raise BookError('epub_structure')
                root = ET.fromstring(archive.read(package))
                manifest = {element.get('id'): element.get('href') for element in root.iter() if element.tag.endswith('}item')}
                names = [manifest.get(element.get('idref')) for element in root.iter() if element.tag.endswith('}itemref')]
                if not names:
                    raise BookError('epub_structure')
                prefix = package.rpartition('/')[0]
                parts = []
                for name in names:
                    if not isinstance(name, str):
                        raise BookError('epub_structure')
                    parsed = urlsplit(name)
                    name = unquote(parsed.path)
                    if (not name or parsed.scheme or parsed.netloc or name.startswith('/') or '\\' in name
                            or '..' in name.split('/') or '\x00' in name):
                        raise BookError('epub_structure')  # never claim an incomplete spine is complete
                    path = prefix + '/' + name if prefix else name
                    soup = BeautifulSoup(archive.read(path), 'html.parser')
                    for tag in soup(['script', 'style', 'iframe', 'object', 'svg']):
                        tag.decompose()
                    parts.append(soup.get_text('\n', strip=True))
                text = '\n\n'.join(parts)
        except (zipfile.BadZipFile, KeyError, ET.ParseError, DefusedXmlException, UnicodeError, RuntimeError):
            raise BookError('epub_structure') from None
    elif format == 'pdf':
        if b'%PDF-' not in raw[:1024]:
            raise BookError('not_a_book_pdf')
        # Isolated bounded child: no bot credentials, PDF actions or JavaScript.
        # A page with no extractable text could be a scan; do not claim a
        # complete reading copy when text from any page is missing.
        script = """import io,sys,resource
resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(12,12))
try:
 from pypdf import PdfReader
except ImportError:
 raise SystemExit(22)
try:
 reader=PdfReader(io.BytesIO(sys.stdin.buffer.read(8000001)))
 if reader.is_encrypted: raise SystemExit(20)
 if len(reader.pages)>600: raise SystemExit(21)
 parts=[];size=0
 for page in reader.pages:
  text=page.extract_text() or ''
  if not text.strip(): raise SystemExit(24)
  size+=len(text.encode('utf-8'))+2
  if size>3000000: raise SystemExit(21)
  parts.append(text)
 sys.stdout.buffer.write(('\\n\\n'.join(parts)).encode('utf-8'))
except Exception:
 raise SystemExit(23)
"""
        try:
            with tempfile.TemporaryDirectory(prefix='michiflin-book-pdf-') as directory:
                result = subprocess.run([sys.executable, '-I', '-c', script], input=raw,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory,
                    env={'PATH': os.defpath, 'HOME': directory, 'LANG': 'C.UTF-8'}, timeout=18, check=False)
            if result.returncode or len(result.stdout) > MAX_TEXT:
                reason = {20: 'protected_book', 21: 'book_too_large', 24: 'pdf_unreadable_pages'}.get(result.returncode, 'pdf_unavailable_or_limit')
                raise BookError(reason)
            text = result.stdout.decode('utf-8')
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            raise BookError('pdf_unavailable_or_limit') from None
    else:
        if not html_soup:
            raise BookError('not_a_book_html')
        for tag in html_soup(['script', 'style', 'nav', 'footer', 'iframe', 'object', 'svg']):
            tag.decompose()
        text = (html_soup.select_one('.mw-parser-output') or html_soup.find('main') or html_soup.find('article') or html_soup.body or html_soup).get_text('\n', strip=True)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text).strip()
    if not text:
        raise BookError('no_readable_text')
    if len(text.encode()) > MAX_TEXT:
        raise BookError('book_too_large')
    is_page = format in {'html', 'htm'}
    return {'text': text, 'format': 'reading_text', 'source_format': format, 'url': url,
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'complete': not is_page,
            'scope': 'page' if is_page else 'document'}
