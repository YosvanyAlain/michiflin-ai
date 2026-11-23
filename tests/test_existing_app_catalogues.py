"""Isolated public-catalogue regressions for the unchanged miniapp backend."""
import io
import json
from unittest import mock

import pytest
from michiflin import book_library as books


def test_gutenberg_search_honors_query_and_never_skips_provider_pages(tmp_path, monkeypatch):
    import sqlite3
    from michiflin.gutenberg_catalog import GutenbergClient
    client = GutenbergClient(tmp_path)
    with sqlite3.connect(client.database) as db:
        db.execute('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,author TEXT,topic TEXT,language TEXT)')
        db.execute('INSERT INTO books VALUES(1,?,?,?,?)', ('declaration of independence', 'author', 'law', ';en;'))
        for identifier in range(2, 17):
            db.execute('INSERT INTO books VALUES(?,?,?,?,?)', (identifier, f'python tutorial {identifier}', 'author', 'programming', ';en;'))
    monkeypatch.setattr(client, 'sync', lambda: None)
    monkeypatch.setattr(client, 'metadata', lambda identifier: {'id': identifier, 'copyright': False})
    params = {'search': 'tutorial de python', 'page': 1}
    first = client.get('https://gutendex.com/books/', params)
    second = client.get('https://gutendex.com/books/', {**params, 'page': 2})
    third = client.get('https://gutendex.com/books/', {**params, 'page': 3})
    ids = [entry['id'] for result in (first, second, third) for entry in result['results']]
    assert ids == list(range(2, 17))
    assert first['count'] == 15 and first['next'] and second['next'] and not third['next']
    assert client.get('https://gutendex.com/books/', {'search': 'no coincidencia'})['results'] == []


def test_combined_search_preserves_each_full_provider_page(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    def fake(source, query, page, language, *_args):
        rows = [{'id': f'{source}:{number}', 'downloads': [{'format': 'txt', 'url': 'https://example.org/test.txt'}]}
                for number in range((page - 1) * 20, page * 20)]
        return rows, page == 1
    monkeypatch.setattr(books, '_search', fake)
    first = books.search('all', 'fixture', 1)
    second = books.search('all', 'fixture', 2)
    assert len(first['books']) == len(second['books']) == 120
    ids = {book['id'] for page in (first, second) for book in page['books']}
    assert len(ids) == 240 and 'archive:19' in ids and 'archive:20' in ids
    assert first['more'] and not second['more']


def test_partial_sources_report_reason_without_caching_the_failure(monkeypatch):
    import requests
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    response = requests.Response()
    response.status_code = 429
    response.headers['Retry-After'] = '60'
    calls = []
    def fake(source, query, page, language, *_args):
        calls.append(source)
        if source == 'google':
            raise requests.HTTPError(response=response)
        return [{'id': source, 'downloads': [{'format': 'txt', 'url': 'https://example.org/test.txt'}]}], False
    monkeypatch.setattr(books, '_search', fake)
    result = books.search('all', 'test')
    assert result['unavailable'] == ['google'] and result['source_errors']['google'] == 'source_rate_limited'
    assert len(result['books']) == 5
    assert not any(json.loads(key)[0] == 'all' for key in books._cache)
    with pytest.raises(books.BookError, match='source_rate_limited'):
        books.search('google', 'test', refresh=True)
    assert calls.count('google') == 1  # refresh does not bypass a rate limit


def test_source_refresh_rechecks_healthy_cache(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    calls = []
    def fake(*_args):
        calls.append(1)
        return [{'id': 'free', 'downloads': [{'format': 'txt', 'url': 'https://example.org/test.txt'}]}], False
    monkeypatch.setattr(books, '_search', fake)
    books.search('google', 'fixture')
    books.search('google', 'fixture')
    books.search('google', 'fixture', refresh=True)
    assert len(calls) == 2


def test_openlibrary_verifies_the_actual_edition_and_ignores_irrelevant_or_loan_records(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    query_urls = []
    def fake_json(url):
        query_urls.append(url)
        if 'openlibrary.org/search.json?' in url:
            return {'numFound': 3, 'docs': [
                {'key': '/works/OL1W', 'title': 'Python para todos', 'author_name': ['Autor'],
                 'ebook_access': 'public', 'ia': ['loan', 'free'], 'subject': ['Python'], 'language': ['spa']},
                {'key': '/works/OL2W', 'title': 'Python protegido', 'ebook_access': 'borrowable', 'ia': ['loan']},
                {'key': '/works/OL3W', 'title': 'Historia de Roma', 'ebook_access': 'public', 'ia': ['free']},
            ]}
        if url.endswith('/loan'):
            return {'metadata': {'collection_set': 'printdisabled'}, 'files': [{'name': 'book_djvu.txt'}]}
        if url.endswith('/free'):
            return {'metadata': {'mediatype': 'texts', 'title': 'Python para todos'},
                    'files': [{'name': 'book_djvu.txt', 'size': '500'}, {'name': 'book.epub', 'size': '900'}]}
        raise AssertionError(url)
    monkeypatch.setattr(books, '_json', fake_json)
    rows, more = books._search('openlibrary', 'tutorial de python', 1, '')
    assert not more and len(rows) == 1
    assert rows[0]['archive_id'] == 'free' and rows[0]['downloads']
    assert all('/free/' in item['url'] for item in rows[0]['downloads'])
    params = parse_qs(urlsplit(query_urls[0]).query)
    assert 'python' in params['q'][0] and 'ebook_access:public' in params['q'][0]


@pytest.mark.parametrize('metadata', [
    {'collection': 'inlibrary'}, {'collection_set': 'printdisabled'},
    {'access-restricted-item': 'true'}, {'external-identifier': ['urn:lcp:book:epub:example']},
])
def test_archive_restricted_metadata_blocks_even_nominally_public_files(monkeypatch, metadata):
    monkeypatch.setattr(books, '_json', lambda _url: {'metadata': metadata, 'files': [{'name': 'book_djvu.txt'}]})
    assert books.archive_download('book') == []
    with pytest.raises(books.BookError, match='source_restricted'):
        books._guard_archive_url('https://archive.org/download/book/book_djvu.txt', {})


def test_archive_repairs_plus_only_against_exact_public_metadata_and_avoids_bundles(monkeypatch):
    data = {'metadata': {'mediatype': 'texts', 'title': 'Python para todos'},
            'files': [{'name': 'Python para todos.pdf', 'size': '4000'}, {'name': 'hidden.epub', 'private': 'true'}]}
    monkeypatch.setattr(books, '_json', lambda _url: data)
    fixed = books._guard_archive_url('https://ia802800.us.archive.org/28/items/book/Python+para+todos.pdf', {})
    assert fixed == 'https://archive.org/download/book/Python%20para%20todos.pdf'
    data['metadata']['title'] = 'Python Ebooks'
    data['files'] = [{'name': f'different_book_{number}.txt'} for number in range(5)]
    assert books.archive_download('book') == []  # never offer an arbitrary file from a collection as one book
    assert len(books.archive_download('book', filename='different_book_2.txt')) == 1


def test_redirect_cannot_bypass_archive_loan_check(monkeypatch):
    class Response:
        status_code = 302
        headers = {'Location': 'https://dn760007.eu.archive.org/0/items/loan/book_djvu.txt'}
        def close(self): pass
        def raise_for_status(self): pass
    request = mock.Mock(return_value=Response())
    monkeypatch.setattr(books.net, 'request', request)
    monkeypatch.setattr(books, '_json', lambda _url: {'metadata': {'collection_set': 'printdisabled'},
                                                       'files': [{'name': 'book_djvu.txt'}]})
    with pytest.raises(books.BookError, match='source_restricted'):
        books._fetch('https://example.org/redirect', books.MAX_DOCUMENT, True)
    assert request.call_count == 1  # restricted file was not fetched


def test_standard_uses_public_search_and_only_real_epub_links(monkeypatch):
    listing = b'''<html><ol class="ebooks-list"><li about="/ebooks/arthur/sherlock">
      <p><span property="schema:name">Sherlock Holmes</span></p>
      <p class="author"><span property="schema:name">Arthur Doyle</span></p></li></ol>
      <nav class="pagination"><a href="/ebooks?page=2">Next</a></nav></html>'''
    detail = b'''<html><a href="https://evil.example/book.epub">bad</a>
      <a href="/ebooks/arthur/sherlock/downloads/arthur_sherlock.epub">download</a></html>'''
    urls = []
    def fake_fetch(url, *_args):
        urls.append(url)
        return (listing if '/ebooks?' in url else detail), 'text/html'
    monkeypatch.setattr(books, '_fetch', fake_fetch)
    rows, more = books._search('standard', 'sherlock', 1, '')
    assert more and len(rows) == 1
    assert rows[0]['downloads'] == [{'format': 'epub', 'url': 'https://standardebooks.org/ebooks/arthur/sherlock/downloads/arthur_sherlock.epub'}]
    assert all('/opds/' not in url for url in urls)


def test_standard_public_download_interstitial_follows_only_the_exact_same_file(monkeypatch):
    class Response:
        status_code = 200
        def __init__(self, data, content_type):
            self.data, self.headers = data, {'content-type': content_type}
        def close(self): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield self.data
    first = b'''<html><title>Your Download Has Started! - Standard Ebooks</title><meta http-equiv="refresh" content="0; url=/ebooks/a/b/downloads/b.epub?source=download"></html>'''
    replies = iter([Response(first, 'text/html'), Response(b'PK\x03\x04test', 'application/epub+zip')])
    request = mock.Mock(side_effect=lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr(books.net, 'request', request)
    raw, kind = books._fetch('https://standardebooks.org/ebooks/a/b/downloads/b.epub', books.MAX_DOCUMENT, True)
    assert raw.startswith(b'PK') and kind == 'application/epub+zip'
    assert request.call_count == 2 and request.call_args_list[1].args[1].endswith('?source=download')


def test_web_search_paginates_candidates_and_does_not_claim_html_is_a_book(monkeypatch):
    from types import SimpleNamespace
    results = [SimpleNamespace(title=f'Python tutorial {n}', url=f'https://example.org/python-{n}', snippet='') for n in range(25)]
    monkeypatch.setattr(books.tools, 'search', lambda *_args, **_kwargs: results)
    pages = [books._search('web', 'tutorial de python', page, '') for page in (1, 2, 3)]
    assert [len(rows) for rows, _ in pages] == [12, 12, 1]
    assert [more for _, more in pages] == [True, True, False]
    assert all(item['verification'] == 'candidate' and item['downloads'][0]['format'] == 'auto' for item in pages[0][0])
    monkeypatch.setattr(books, '_fetch', lambda *_args: (b'<html><main>One page<script>hidden()</script></main></html>', 'text/html'))
    result = books.reading_copy('https://example.org/page', 'auto')
    assert result['scope'] == 'page' and result['complete'] is False
    assert result['text'] == 'One page' and 'hidden' not in result['text']


def test_download_rejects_fake_formats_and_login_html(monkeypatch):
    monkeypatch.setattr(books, '_fetch', lambda *_args: (b'<html><main>Catalogue page</main></html>', 'text/html'))
    for format, code in [('txt', 'not_a_book_text'), ('pdf', 'not_a_book_pdf'), ('epub', 'not_a_book_epub')]:
        with pytest.raises(books.BookError, match=code):
            books.reading_copy('https://example.org/file', format)
    monkeypatch.setattr(books, '_fetch', lambda *_args: (b'<html><form><input type="password"></form></html>', 'text/html'))
    with pytest.raises(books.BookError, match='source_auth_required'):
        books.reading_copy('https://example.org/login', 'auto')


def test_encrypted_or_unreadable_pdf_is_not_saved_as_complete(monkeypatch):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(300, 300)
    blank = io.BytesIO(); writer.write(blank)
    monkeypatch.setattr(books, '_fetch', lambda *_args: (blank.getvalue(), 'application/pdf'))
    with pytest.raises(books.BookError, match='pdf_unreadable_pages'):
        books.reading_copy('https://example.org/scan.pdf', 'pdf')
    writer.encrypt('fixture')
    encrypted = io.BytesIO(); writer.write(encrypted)
    monkeypatch.setattr(books, '_fetch', lambda *_args: (encrypted.getvalue(), 'application/pdf'))
    with pytest.raises(books.BookError, match='protected_book'):
        books.reading_copy('https://example.org/protected.pdf', 'pdf')


def test_tutorial_query_does_not_treat_comedy_scripts_as_programming(monkeypatch):
    from michiflin import book_search
    query = 'tutorial de python'
    assert book_search.relevance(query, "Monty Python's Flying Circus The Sketches", subjects=['Television', 'TV Script']) < 0
    assert book_search.relevance(query, 'Python para todos', subjects=['Programming languages']) >= 0
    assert book_search.relevance('guía Monty Python', "Monty Python's Flying Circus The Sketches") >= 0


def test_web_search_empty_is_not_cached_as_a_definitive_answer(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books.tools, 'search', mock.Mock(return_value=[]))
    monkeypatch.setattr(books.tools, 'search_limited', lambda: False)
    first = books.search('web', 'python')
    second = books.search('web', 'python')
    assert first['warnings']['web'] == ['search_empty_or_unavailable']
    assert second['warnings'] == first['warnings'] and books.tools.search.call_count == 2


def test_combined_search_deduplicates_only_the_same_archive_edition(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    def fake(source, *_args):
        if source not in {'openlibrary', 'archive'}:
            return [], False
        return [{'id': source + ':1', 'archive_id': 'same-edition', '_rank': 1,
                 'downloads': [{'format': 'txt', 'url': 'https://archive.org/download/same-edition/book.txt'}]}], False
    monkeypatch.setattr(books, '_search', fake)
    response = books.search('all', 'book')
    assert len(response['books']) == 1 and response['books'][0]['archive_id'] == 'same-edition'


def test_archive_requires_verified_text_metadata_and_matching_identifier(monkeypatch):
    data = {'files': [{'name': 'book.txt'}]}
    monkeypatch.setattr(books, '_json', lambda _url: data)
    assert books.archive_download('book') == []
    data['metadata'] = {'mediatype': 'audio'}
    assert books.archive_download('book') == []
    data['metadata'] = {'mediatype': 'texts', 'identifier': 'different'}
    assert books.archive_download('book') == []
    data['metadata']['identifier'] = 'book'
    data['is_dark'] = 'true'
    assert books.archive_download('book') == []
    data['is_dark'] = 'false'
    assert len(books.archive_download('book')) == 1


def test_archive_metadata_rate_limit_pauses_queued_calls_across_catalogues(monkeypatch):
    import requests
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    response = requests.Response(); response.status_code = 429
    response.headers['Retry-After'] = '60'
    call = mock.Mock(side_effect=requests.HTTPError(response=response))
    monkeypatch.setattr(books, '_json', call)
    with pytest.raises(requests.HTTPError):
        books.archive_download('first')
    with pytest.raises(books.BookError, match='source_rate_limited'):
        books.archive_download('second')
    call.assert_called_once()  # refreshing a different source cannot bypass this pause


def test_combined_cursor_retains_failed_and_partially_checked_provider_pages(monkeypatch):
    import requests
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    calls = []
    def fake(source, query, page, language, diagnostics, locale):
        calls.append((source, page))
        if source == 'google' and page == 1 and calls.count(('google', 1)) == 1:
            raise requests.Timeout()
        if source == 'archive' and page == 1 and calls.count(('archive', 1)) == 1:
            diagnostics.append('metadata_incomplete')
        record = {'id': f'{source}:{page}', 'source': source, 'title': f'{source} {page}',
                  'downloads': [{'format': 'txt', 'url': 'https://example.org/test.txt'}]}
        return [record], page == 1
    monkeypatch.setattr(books, '_search', fake)
    first = books.search('all', 'fixture')
    assert first['unavailable'] == ['google']
    assert first['source_pages']['google'] == first['source_pages']['archive'] == 1
    assert first['source_pages']['gutenberg'] == 2
    second = books.search('all', 'fixture', 2, source_pages=first['source_pages'])
    assert ('google', 1) in calls and calls.count(('archive', 1)) == 2
    assert second['source_pages']['google'] == second['source_pages']['archive'] == 2
    assert second['source_pages']['gutenberg'] is None
    third = books.search('all', 'fixture', 3, source_pages=second['source_pages'])
    unique = {row['id'] for result in (first, second, third) for row in result['books']}
    assert len(unique) == len(books.COMBINED_SOURCES) * 2
    assert 'google:1' in unique and 'google:2' in unique
    assert not third['more'] and all(value is None for value in third['source_pages'].values())


def test_single_source_partial_metadata_retries_same_page(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    def fake(source, query, page, language, diagnostics, locale):
        diagnostics.append('metadata_incomplete')
        return [], False
    monkeypatch.setattr(books, '_search', fake)
    result = books.search('archive', 'fixture', 3)
    assert result['more'] and result['retry_page'] == 3
    assert result['warnings'] == {'archive': ['metadata_incomplete']}


@pytest.mark.parametrize('cursor', [None, {}, {'unknown': 1}, 'not-a-map', {'gutenberg': True}])
def test_invalid_combined_cursor_is_rejected_without_contacting_providers(monkeypatch, cursor):
    if cursor is None:
        cursor = {source: True if source == 'google' else 1 for source in books.COMBINED_SOURCES}
    fetch = mock.Mock(); monkeypatch.setattr(books, '_search', fetch)
    with pytest.raises(books.BookError, match='invalid_query'):
        books.search('all', 'fixture', source_pages=cursor)
    fetch.assert_not_called()


def test_openlibrary_does_not_attach_a_different_archive_title_even_with_same_author(monkeypatch):
    def fake_json(url):
        if 'openlibrary.org/search.json?' in url:
            return {'numFound': 1, 'docs': [{'key': '/works/OL1W', 'title': 'Python How-To',
                'author_name': ['Yong Cui'], 'ebook_access': 'public', 'ia': ['wrong', 'right'],
                'subject': ['Python']} ]}
        if url.endswith('/wrong'):
            return {'metadata': {'mediatype': 'texts', 'title': "Thomas D'Arcy McGee, a popular lecture",
                                 'creator': 'Cui, Yong'}, 'files': [{'name': 'lecture.txt'}]}
        if url.endswith('/right'):
            return {'metadata': {'mediatype': 'texts', 'title': 'Python How-To', 'creator': 'Yong Cui'},
                    'files': [{'name': 'python.txt'}]}
        raise AssertionError(url)
    monkeypatch.setattr(books, '_json', fake_json)
    rows, _ = books._search('openlibrary', 'tutorial de python', 1, '')
    assert len(rows) == 1 and rows[0]['archive_id'] == 'right' and rows[0]['title_checked']
    assert '/right/' in rows[0]['downloads'][0]['url']


def test_title_relevance_uses_whole_words_and_checks_edition_numbers():
    from michiflin import book_search
    assert book_search.relevance('tutorial de python', 'The pythonism of the present day', subjects=['Spiritualism']) < 0
    assert book_search.relevance('java', 'Learn JavaScript') < 0
    assert book_search.relevance('python', 'Python: the language') >= 0
    assert not book_search.same_edition('Python 3 Guide', 'Python 2 Guide', [], [])
    assert not book_search.same_edition('Python How-To', 'A popular lecture', ['Yong Cui'], 'Cui, Yong')
    assert book_search.same_edition('Python Guide Documentation', 'Python Guide Documentation', ['Kenneth Reitz'], 'Kenneth Keitz')


def test_gutenberg_search_does_not_match_longer_different_word(tmp_path, monkeypatch):
    import sqlite3
    from michiflin.gutenberg_catalog import GutenbergClient
    client = GutenbergClient(tmp_path)
    with sqlite3.connect(client.database) as db:
        db.execute('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,author TEXT,topic TEXT,language TEXT)')
        db.execute('INSERT INTO books VALUES(1,?,?,?,?)', ('pythonism', 'author', 'spiritualism', ';en;'))
        db.execute('INSERT INTO books VALUES(2,?,?,?,?)', ('python tutorial', 'author', 'programming', ';en;'))
    monkeypatch.setattr(client, 'sync', lambda: None)
    monkeypatch.setattr(client, 'metadata', lambda identifier: {'id': identifier, 'copyright': False})
    result = client.get('https://gutendex.com/books/', {'search': 'python'})
    assert [row['id'] for row in result['results']] == [2]


def test_last_page_still_allows_retry_of_failed_or_incomplete_source(monkeypatch):
    import requests
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    def fake(source, query, page, language, diagnostics, locale):
        if source == 'google':
            raise requests.Timeout()
        diagnostics.append('metadata_incomplete')
        return [], False
    monkeypatch.setattr(books, '_search', fake)
    pages = {source: 100 if source == 'google' else None for source in books.COMBINED_SOURCES}
    combined = books.search('all', 'fixture', 100, source_pages=pages)
    assert combined['more'] and combined['source_pages']['google'] == 100
    single = books.search('archive', 'fixture', 100)
    assert single['more'] and single['retry_page'] == 100


def test_archive_does_not_present_small_ambiguous_multi_book_item_as_one_book(monkeypatch):
    data = {'metadata': {'identifier': 'unrelated-item', 'mediatype': 'texts', 'title': 'Unrelated title'},
            'files': [{'name': 'first-novel.txt'}, {'name': 'second-novel.epub'}]}
    monkeypatch.setattr(books, '_json', lambda _url: data)
    assert books.archive_download('unrelated-item') == []
    data['metadata']['title'] = 'first novel'
    assert [item['format'] for item in books.archive_download('unrelated-item')] == ['txt']
    data['metadata']['title'] = 'A collection of novels'
    assert books.archive_download('unrelated-item') == []


def test_retry_after_honors_long_numeric_and_http_date_without_shortening_an_existing_pause(monkeypatch):
    import math
    import time
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    import requests
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    response = requests.Response(); response.status_code = 429
    response.headers['Retry-After'] = '7200'
    before = time.monotonic()
    books._pause_after_http('google', requests.HTTPError(response=response))
    assert books._source_pause['google'] >= before + 7199
    response.headers['Retry-After'] = format_datetime(datetime.now(timezone.utc) + timedelta(hours=4), usegmt=True)
    books._pause_after_http('google', requests.HTTPError(response=response))
    longer = books._source_pause['google']
    assert longer >= time.monotonic() + 14395
    response.headers['Retry-After'] = '60'
    books._pause_after_http('google', requests.HTTPError(response=response))
    assert books._source_pause['google'] == longer
    response.headers['Retry-After'] = '9' * 20
    books._pause_after_http('google', requests.HTTPError(response=response))
    assert math.isinf(books._source_pause['google'])  # never silently bypass an implausibly long hold


def test_gutenberg_http_restriction_is_preserved_for_shared_rate_limit(monkeypatch):
    from michiflin import gutenberg_catalog as catalog
    import requests
    class Reply:
        status_code = 429
        headers = {'Retry-After': '7200'}
        closed = False
        def raise_for_status(self): raise requests.HTTPError(response=self)
        def close(self): self.closed = True
    reply = Reply()
    monkeypatch.setattr(catalog.net, 'request', lambda *_args, **_kwargs: reply)
    with pytest.raises(catalog.AppError) as caught:
        catalog.fetch(catalog.FEED, limit=100)
    assert caught.value.code == 'source_rate_limited' and caught.value.response is reply and reply.closed
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    books._pause_after_http('gutenberg', caught.value)
    assert books._paused_reason('gutenberg') == 'source_rate_limited'


def test_gutenberg_does_not_bypass_restrictions_using_a_stale_index_or_queued_metadata(tmp_path, monkeypatch):
    import sqlite3
    import requests
    from michiflin.gutenberg_catalog import AppError, GutenbergClient
    client = GutenbergClient(tmp_path)
    with sqlite3.connect(client.database) as db:
        db.execute('CREATE TABLE books(id INTEGER PRIMARY KEY,title TEXT,author TEXT,topic TEXT,language TEXT)')
        for identifier in range(1, 7):
            db.execute('INSERT INTO books VALUES(?,?,?,?,?)', (identifier, 'sherlock holmes', 'doyle', 'fiction', ';en;'))
    response = requests.Response(); response.status_code = 429; response.headers['Retry-After'] = '60'
    denied = AppError('source_rate_limited', retryable=True, response=response)
    monkeypatch.setattr(client, 'sync', mock.Mock(side_effect=denied))
    with pytest.raises(AppError, match='source_rate_limited'):
        client.get('https://gutendex.com/books/', {'search': 'sherlock'})
    monkeypatch.setattr(client, 'sync', lambda: None)
    metadata = mock.Mock(side_effect=denied)
    monkeypatch.setattr(client, 'metadata', metadata)
    with pytest.raises(AppError) as caught:
        client.get('https://gutendex.com/books/', {'search': 'sherlock'})
    assert caught.value.response is response and 1 <= metadata.call_count <= 3


@pytest.mark.parametrize('key', ['access-restricted-item', 'is_restricted', 'is_dark', 'lending_enabled'])
def test_archive_blocks_top_level_restriction_markers_and_private_file_flags(monkeypatch, key):
    data = {'metadata': {'mediatype': 'texts', 'identifier': 'item'}, key: ' true ',
            'files': [{'name': 'item.txt'}]}
    monkeypatch.setattr(books, '_json', lambda _url: data)
    assert books.archive_download('item') == []
    data.pop(key)
    data['files'][0]['private'] = ' TRUE '
    assert books.archive_download('item') == []
