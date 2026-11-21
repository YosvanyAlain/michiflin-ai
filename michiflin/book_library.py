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
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from . import net, tools

SOURCES = {'gutenberg', 'openlibrary', 'archive', 'google', 'standard', 'wikisource', 'web', 'all'}
MAX_DOCUMENT = 8_000_000
MAX_TEXT = 3_000_000
_cache: dict[str, tuple[float, object]] = {}
_source_pause: dict[str, float] = {}


class BookError(ValueError):
    pass


def _fetch(url: str, maximum: int = MAX_DOCUMENT) -> tuple[bytes, str]:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or any(key in parsed.query.lower() for key in ['api_key=', 'access_token=', 'password='])):
        raise BookError('public_https_required')
    response = net.request('GET', url, stream=True, timeout=(10, 20))
    try:
        response.raise_for_status()
        return net.read_limited(response, maximum), response.headers.get('content-type', '').lower()
    finally:
        response.close()


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
        _gutenberg_client = GutenbergClient(Path(config.TEMP_ROOT) / 'public-book-catalog')
    return _gutenberg_client.get('https://gutendex.com/books/', {'search': query, 'page': page,
        'copyright': 'false', **({'languages': language} if language else {})})


def _row(source: str, identifier: str, title: str, authors: list, url: str, **extra) -> dict:
    return {'id': source + ':' + str(identifier), 'source': source, 'title': str(title)[:300],
            'authors': [str(author)[:160] for author in authors[:8]], 'url': url,
            'cover_url': '', 'downloads': [], 'access': 'catalogue', **extra}


def search(source: str, query: str, page: int = 1, language: str = '') -> dict:
    if source not in SOURCES or not isinstance(query, str) or len(query) > 200 or type(page) is not int or not 1 <= page <= 100:
        raise BookError('invalid_query')
    query = query.strip()
    cache_key = json.dumps([source, query, page, language])
    hit = _cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    if source == 'all':
        books, unavailable, has_more = [], [], False
        def one(name):
            try:
                return name, search(name, query, page, language)
            except Exception:
                return name, None
        with ThreadPoolExecutor(max_workers=5) as pool:
            for name, result in pool.map(one, ['gutenberg', 'openlibrary', 'archive', 'google', 'standard', 'wikisource']):
                if result is None:
                    unavailable.append(name)
                else:
                    books.extend(result['books'][:6])
                    has_more = has_more or bool(result.get('more'))
        result = {'books': books, 'page': page, 'more': has_more, 'unavailable': unavailable}
    else:
        if time.monotonic() < _source_pause.get(source, 0):
            raise BookError('source_temporarily_unavailable')
        try:
            books, more = _search(source, query, page, language)
        except Exception as exc:
            response = getattr(exc, 'response', None)
            if response is not None and getattr(response, 'status_code', 0) in {403, 429}:
                retry = str(response.headers.get('Retry-After', '300'))
                wait = max(60, min(3600, int(retry))) if retry.isdigit() else 300
                _source_pause[source] = time.monotonic() + wait
            raise
        result = {'books': books, 'page': page, 'more': more, 'unavailable': []}
    _cache[cache_key] = (time.monotonic(), result)
    while len(_cache) > 64:
        _cache.pop(next(iter(_cache)))
    return result


def _search(source: str, query: str, page: int, language: str) -> tuple[list, bool]:
    books = []
    if source == 'gutenberg':
        data = _gutenberg(query, page, language)
        for row in data.get('results', [])[:24]:
            if not isinstance(row, dict) or row.get('copyright') is not False or type(row.get('id')) is not int:
                continue
            downloads = [{'format': 'txt' if mime.startswith('text/plain') else 'epub', 'url': url}
                         for mime, url in row.get('formats', {}).items() if mime.startswith(('text/plain', 'application/epub+zip')) and isinstance(url, str) and url.startswith('https://')]
            books.append(_row(source, str(row['id']), row.get('title', ''), [a.get('name', '') for a in row.get('authors', []) if isinstance(a, dict)],
                              f"https://www.gutenberg.org/ebooks/{row['id']}", downloads=downloads,
                              cover_url=row.get('formats', {}).get('image/jpeg', ''), access='public_domain_us', languages=row.get('languages', [])))
        return books, bool(data.get('next'))
    if source == 'openlibrary':
        params = {'q': query or 'subject:literature', 'page': page, 'limit': 20,
                  'fields': 'key,title,author_name,first_publish_year,cover_i,ebook_access,ia'}
        data = _json('https://openlibrary.org/search.json?' + urlencode(params))
        for row in data.get('docs', [])[:20]:
            key = str(row.get('key', '')).rsplit('/', 1)[-1]
            if not re.fullmatch(r'OL\d+W', key):
                continue
            cover = row.get('cover_i')
            books.append(_row(source, key, row.get('title', ''), row.get('author_name', []), 'https://openlibrary.org/works/' + key,
                              cover_url=f'https://covers.openlibrary.org/b/id/{cover}-M.jpg' if type(cover) is int else '',
                              access=str(row.get('ebook_access', 'catalogue')), year=row.get('first_publish_year'),
                              archive_id=(row.get('ia') or [''])[0] if row.get('ebook_access') == 'public' else ''))
        return books, page * 20 < int(data.get('numFound', 0))
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
            books.append(_row(source, identifier, volume.get('title', ''), volume.get('authors', []),
                              'https://books.google.com/books?id=' + identifier, cover_url=cover,
                              downloads=downloads, access=str(access.get('accessViewStatus', 'catalogue')),
                              description=str(volume.get('description', ''))[:3000]))
        return books, page * 20 < int(data.get('totalItems', 0))
    if source == 'archive':
        clean = re.sub(r'[^\w\s-]', ' ', query, flags=re.UNICODE).strip()
        params = [('q', 'mediatype:texts AND (' + (clean or 'public domain') + ')'), ('output', 'json'), ('rows', '20'), ('page', str(page)),
                  ('fl[]', 'identifier'), ('fl[]', 'title'), ('fl[]', 'creator'), ('fl[]', 'licenseurl')]
        data = _json('https://archive.org/advancedsearch.php?' + urlencode(params))
        response = data.get('response', {})
        for row in response.get('docs', [])[:20]:
            identifier = str(row.get('identifier', ''))
            if not re.fullmatch(r'[\w.-]{1,160}', identifier):
                continue
            author = row.get('creator', [])
            books.append(_row(source, identifier, row.get('title', ''), author if isinstance(author, list) else [author],
                              'https://archive.org/details/' + quote(identifier),
                              cover_url='https://archive.org/services/img/' + quote(identifier), access='check_provider', archive_id=identifier))
        return books, page * 20 < int(response.get('numFound', 0))
    if source == 'standard':
        feed, _ = _fetch('https://standardebooks.org/opds/all', 7_000_000)
        tree = ET.fromstring(feed)
        matches = []
        atom = '{http://www.w3.org/2005/Atom}'
        for entry in tree.findall(atom + 'entry'):
            title = entry.findtext(atom + 'title', '')
            authors = [author.findtext(atom + 'name', '') for author in entry.findall(atom + 'author')]
            if query and query.casefold() not in (title + ' ' + ' '.join(authors)).casefold():
                continue
            url = entry.findtext(atom + 'id', '')
            if not url.startswith('https://standardebooks.org/ebooks/'):
                continue
            links = entry.findall(atom + 'link')
            downloads = [{'format': 'epub', 'url': urljoin(url, link.get('href', ''))} for link in links if link.get('type', '').startswith('application/epub+zip')]
            cover = next((urljoin(url, link.get('href', '')) for link in links if 'image' in link.get('rel', '') and link.get('type', '').startswith('image/')), '')
            matches.append(_row(source, hashlib.sha256(url.encode()).hexdigest()[:24], title, authors, url,
                                downloads=downloads, cover_url=cover, access='public_domain_us'))
        return matches[(page - 1) * 20:page * 20], page * 20 < len(matches)
    if source == 'wikisource':
        language = 'en' if language == 'en' else 'es'
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
            books.append(_row(source, str(identifier), title, [], url, languages=[language],
                downloads=[{'format': 'html', 'url': url}], access='public_reading_page',
                description=str(row.get('extract', ''))[:3000] + '\nPágina de Wikisource; puede contener enlaces a otros capítulos.'))
        return books, bool(data.get('continue'))
    if source == 'web':
        results = tools.search(query + ' libro ebook dominio público', 'en' if language == 'en' else 'es', limit=12)
        for item in results:
            url = str(item.url)
            if not url.startswith('https://'):
                continue
            suffix = urlsplit(url).path.rsplit('.', 1)[-1].lower()
            books.append(_row(source, hashlib.sha256(url.encode()).hexdigest()[:24], item.title, [], url,
                              downloads=[{'format': suffix, 'url': url}] if suffix in {'txt', 'epub', 'pdf', 'html', 'htm'} else [],
                              description=item.snippet, access='verify_rights'))
        return books, False
    raise BookError('source')


def archive_download(identifier: str) -> list[dict]:
    if not re.fullmatch(r'[\w.-]{1,160}', identifier):
        raise BookError('identifier')
    data = _json('https://archive.org/metadata/' + quote(identifier))
    metadata = data.get('metadata', {})
    collections = metadata.get('collection', [])
    if isinstance(collections, str):
        collections = [collections]
    if (str(metadata.get('access-restricted-item', '')).lower() == 'true'
            or any(any(flag in str(value).lower() for flag in ['borrow', 'printdisabled', 'inlibrary']) for value in collections)):
        return []
    candidates = []
    for file in data.get('files', [])[:1000]:
        name = str(file.get('name', ''))
        if file.get('private') in (True, 'true') or '/' in name or '\\' in name:
            continue
        format = 'txt' if name.endswith(('_djvu.txt', '.txt')) else 'epub' if name.endswith('.epub') else ''
        if format:
            candidates.append({'format': format, 'url': 'https://archive.org/download/' + quote(identifier) + '/' + quote(name)})
    return sorted(candidates, key=lambda item: item['format'] != 'txt')[:4]


def reading_copy(url: str, format: str) -> dict:
    if format not in {'txt', 'epub', 'pdf', 'html', 'htm'}:
        raise BookError('format')
    raw, content_type = _fetch(url)
    if format == 'txt':
        if 'html' in content_type or raw.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            raise BookError('not_a_book_text')
        text = raw.decode('utf-8-sig', errors='replace')
    elif format == 'epub':
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 3000 or sum(item.file_size for item in infos) > 24_000_000:
                raise BookError('book_too_large')
            if any('encryption.xml' in item.filename for item in infos):
                raise BookError('protected_book')
            container = ET.fromstring(archive.read('META-INF/container.xml'))
            package = next(element.get('full-path') for element in container.iter() if element.tag.endswith('rootfile'))
            if not package or package.startswith('/') or '..' in package.split('/'):
                raise BookError('epub_structure')
            root = ET.fromstring(archive.read(package))
            manifest = {element.get('id'): element.get('href') for element in root.iter() if element.tag.endswith('}item')}
            names = [manifest.get(element.get('idref')) for element in root.iter() if element.tag.endswith('}itemref')]
            prefix = package.rpartition('/')[0]
            parts = []
            for name in names:
                if not name or name.startswith('/') or '..' in name.split('/'):
                    continue
                path = prefix + '/' + name if prefix else name
                soup = BeautifulSoup(archive.read(path), 'html.parser')
                for tag in soup(['script', 'style', 'iframe', 'object', 'svg']):
                    tag.decompose()
                parts.append(soup.get_text('\n', strip=True))
            text = '\n\n'.join(parts)
    elif format == 'pdf':
        # Parsing PDFs is bounded separately from the bot process. No bot
        # credentials are inherited and no PDF actions or JavaScript are run.
        script = """import io,sys,resource
resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(12,12))
from pypdf import PdfReader
reader=PdfReader(io.BytesIO(sys.stdin.buffer.read(8000001)))
if reader.is_encrypted or len(reader.pages)>600: raise SystemExit(2)
parts=[];size=0
for page in reader.pages:
 text=page.extract_text() or ''
 size+=len(text.encode('utf-8'))
 if size>3000000: raise SystemExit(2)
 parts.append(text)
sys.stdout.buffer.write(('\\n\\n'.join(parts)).encode('utf-8'))
"""
        try:
            with tempfile.TemporaryDirectory(prefix='michiflin-book-pdf-') as directory:
                result = subprocess.run([sys.executable, '-I', '-c', script], input=raw,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory,
                    env={'PATH': os.defpath, 'HOME': directory, 'LANG': 'C.UTF-8'}, timeout=18, check=False)
            if result.returncode or len(result.stdout) > MAX_TEXT:
                raise BookError('pdf_unavailable_or_limit')
            text = result.stdout.decode('utf-8')
        except (subprocess.TimeoutExpired, OSError):
            raise BookError('pdf_unavailable_or_limit') from None
    else:
        soup = BeautifulSoup(raw, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'iframe', 'object', 'svg']):
            tag.decompose()
        text = (soup.select_one('.mw-parser-output') or soup.find('main') or soup.find('article') or soup.body or soup).get_text('\n', strip=True)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text).strip()
    if not text:
        raise BookError('no_readable_text')
    if len(text.encode()) > MAX_TEXT:
        raise BookError('book_too_large')
    return {'text': text, 'format': 'reading_text', 'source_format': format, 'url': url,
            'sha256': hashlib.sha256(text.encode()).hexdigest(), 'complete': True}
