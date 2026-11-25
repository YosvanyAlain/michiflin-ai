"""Library backend and strict compatibility checks for the updated package.

All bridge data, identities and books here are synthetic. No production state,
mail, external credentials or real user library are accessed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
import requests

from michiflin import app_package, book_legacy, book_library as books, config, miniapp


@pytest.fixture(autouse=True)
def isolate_book_runtime(monkeypatch):
    monkeypatch.setattr(books, '_cache', {})
    monkeypatch.setattr(books, '_source_pause', {})
    monkeypatch.setattr(books, '_source_pause_reason', {})
    monkeypatch.setattr(book_legacy, '_sessions', {})
    monkeypatch.setattr(book_legacy, '_alternatives', {})


def _book(source, number, *, identifier=None, archive_id=''):
    return {'id': identifier or f'{source}:{number}', 'source': source, 'title': f'{source} {number}',
            'authors': [], 'url': 'https://example.org/record', 'archive_id': archive_id,
            'downloads': [{'format': 'txt', 'url': f'https://example.org/{source}{number}.txt'}]}


def test_updated_package_keeps_strict_release_fencing(monkeypatch):
    # Only the intended reader, UI, export and Live files change; the offline
    # engines are explicit new assets. Never authorize a prior build in current-only mode.
    manifest = json.loads(Path('tests/fixtures/library-current-package.json').read_text())
    entries = app_package.entries(miniapp.APP_FILES)
    names = {name for name, _ in entries}
    assert set(manifest) <= names
    assert names - set(manifest) == {'book-epub-worker.js', 'book-original.js',
        'vendor/books/fflate.min.js', 'vendor/books/jszip.min.js', 'vendor/books/epub.min.js',
        'vendor/books/purify.min.js', 'vendor/books/pdf.min.mjs', 'vendor/books/pdf.worker.min.mjs',
        'vendor/books/NOTICE.txt', 'vendor/books/LICENSE-fflate.txt', 'vendor/books/LICENSE-jszip.txt',
        'vendor/books/LICENSE-epubjs.txt', 'vendor/books/LICENSE-dompurify.txt', 'vendor/books/LICENSE-pdfjs.txt'}
    changed = {name for name, source in entries if name in manifest
               and hashlib.sha256((miniapp.APP_DIR / source).read_bytes()).hexdigest() != manifest[name]}
    # core.js changes the host-aware export chooser and cancellation handling.
    assert changed == {'app.css', 'app.js', 'books.js', 'core.js', 'i18n.js', 'index.html', 'live.js'}
    monkeypatch.setattr(config, 'BOT_EPOCH', 'test-epoch')
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'fixture-release')
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', True)
    current = miniapp.app_build()
    assert current != 'd7437b1b88'
    assert miniapp.compatible_build({'epoch': 'test-epoch', 'build': current})
    assert not miniapp.compatible_build({'epoch': 'test-epoch', 'build': 'd7437b1b88'})
    assert not miniapp.compatible_build({'epoch': 'another-epoch', 'build': current})
    assert not miniapp.compatible_build({'epoch': 'test-epoch', 'build': 'retired000'})
    monkeypatch.setattr(config, 'MINIAPP_RELEASE', 'public-private-20261119-v1')
    release = miniapp.app_build()
    assert release != '8cab451a13'
    assert miniapp.compatible_build({'epoch': 'test-epoch', 'build': release})
    assert not miniapp.compatible_build({'epoch': 'test-epoch', 'build': '8cab451a13'})


def test_legacy_combined_page_click_retries_failed_source_without_skipping(monkeypatch):
    scope = 'a' * 64
    calls = []
    def fake(source, query, number, language, diagnostics, locale):
        calls.append((source, number))
        if source == 'google' and number == 1 and calls.count(('google', 1)) == 1:
            raise requests.Timeout()
        return [_book(source, number)], number == 1
    monkeypatch.setattr(books, '_search', fake)
    first = book_legacy.search(scope, 'all', 'fixture', 1)
    assert first['more'] and 'Google Books' in ' '.join(first['unavailable'])
    assert ('wikisource', 1) not in calls  # its HTML cannot be labelled honestly by the old UI
    second = book_legacy.search(scope, 'all', 'fixture', 2)
    assert calls.count(('google', 1)) == 2 and ('gutenberg', 2) in calls
    assert any(row['id'] == 'google:1' for row in second['books'])
    third = book_legacy.search(scope, 'all', 'fixture', 3)
    assert [row['id'] for row in third['books']] == ['google:2'] and not third['more']
    assert len({row['id'] for result in (first, second, third) for row in result['books']}) == 10


def test_legacy_partial_single_source_holds_page_and_suppresses_duplicate_cards(monkeypatch):
    scope = 'b' * 64
    calls = []
    def fake(source, query, number, language, diagnostics, locale):
        calls.append(number)
        if len(calls) == 1:
            diagnostics.append('metadata_incomplete')
        return [_book(source, number)], number == 1
    monkeypatch.setattr(books, '_search', fake)
    first = book_legacy.search(scope, 'archive', 'fixture', 1)
    second = book_legacy.search(scope, 'archive', 'fixture', 2)
    third = book_legacy.search(scope, 'archive', 'fixture', 3)
    assert calls == [1, 1, 2]
    assert first['more'] and 'fichas' in ' '.join(first['unavailable'])
    assert not second['books'] and second['more']
    assert [row['id'] for row in third['books']] == ['archive:2']


def test_legacy_direct_source_rate_limit_keeps_page_and_never_retries_automatically(monkeypatch):
    scope = 'c' * 64
    response = requests.Response(); response.status_code = 429; response.headers['Retry-After'] = '60'
    fetch = mock.Mock(side_effect=requests.HTTPError(response=response))
    monkeypatch.setattr(books, '_search', fetch)
    first = book_legacy.search(scope, 'google', 'fixture', 1)
    second = book_legacy.search(scope, 'google', 'fixture', 2)
    assert first['more'] and second['more'] and 'límite temporal' in ' '.join(first['unavailable'])
    assert fetch.call_count == 1  # explicit More still respects the provider pause


def test_legacy_session_is_bound_to_instance_and_detects_expired_or_skipped_pages(monkeypatch):
    scope, other = 'd' * 64, 'e' * 64
    fetch = mock.Mock(return_value=([_book('archive', 1)], True))
    monkeypatch.setattr(books, '_search', fetch)
    first = book_legacy.search(scope, 'archive', 'fixture', 1)
    assert first['books']
    skipped = book_legacy.search(scope, 'archive', 'fixture', 3)
    foreign = book_legacy.search(other, 'archive', 'fixture', 2)
    assert not skipped['more'] and not foreign['books']
    assert 'borra solo la caché' in skipped['unavailable'][0]
    assert fetch.call_count == 1
    key = book_legacy._session_key(scope, 'archive', 'fixture', '', 'es')
    book_legacy._sessions[key].at = time.monotonic() - book_legacy._SESSION_TTL - 1
    expired = book_legacy.search(scope, 'archive', 'fixture', 2)
    assert not expired['more'] and 'Paginación caducada' in expired['unavailable'][0]
    assert fetch.call_count == 1


def test_legacy_deduplicates_same_edition_from_distinct_catalogues(monkeypatch):
    def fake(source, query, number, language, diagnostics, locale):
        if source == 'openlibrary':
            return ([_book(source, number, archive_id='same-edition')] if number == 1 else []), number == 1
        if source == 'archive':
            return ([_book(source, number, archive_id='same-edition')] if number == 2 else []), number == 1
        return [], False
    monkeypatch.setattr(books, '_search', fake)
    scope = 'f' * 64
    first = book_legacy.search(scope, 'all', 'fixture', 1)
    second = book_legacy.search(scope, 'all', 'fixture', 2)
    assert len(first['books']) == 1 and not second['books']


def test_legacy_hides_unverified_links_and_html_instead_of_calling_them_complete_books(monkeypatch):
    allowed = _book('web', 1, identifier='web:verified', archive_id='verified')
    allowed.update(verification='candidate', metadata_checked=True)
    unverified = {**_book('web', 2), 'verification': 'candidate'}
    html = {**_book('web', 3), 'downloads': [{'format': 'html', 'url': 'https://example.org/index.html'}]}
    auto = {**_book('web', 4), 'downloads': [{'format': 'auto', 'url': 'https://example.org/index'}]}
    monkeypatch.setattr(books, '_search', lambda *_args: ([allowed, unverified, html, auto], False))
    result = book_legacy.search('1' * 64, 'web', 'fixture', 1)
    assert [row['id'] for row in result['books']] == ['web:verified']
    blocked = book_legacy.search('1' * 64, 'wikisource', 'fixture', 1)
    assert not blocked['books'] and 'formato verificable' in blocked['unavailable'][0]


def test_download_fallback_is_bounded_same_record_same_scope_and_never_bypasses_403_or_drm(monkeypatch):
    scope, other = '2' * 64, '3' * 64
    row = _book('archive', 1)
    row['downloads'] = [{'format': 'txt', 'url': 'https://example.org/a.txt'},
                        {'format': 'epub', 'url': 'https://example.org/a.epub'},
                        {'format': 'pdf', 'url': 'https://example.org/a.pdf'}]
    monkeypatch.setattr(books, '_search', lambda *_args: ([row], False))
    book_legacy.search(scope, 'archive', 'fixture', 1)
    missing = requests.Response(); missing.status_code = 404
    forbidden = requests.Response(); forbidden.status_code = 403
    read = mock.Mock(side_effect=[requests.HTTPError(response=missing), {'text': 'Contenido', 'complete': True}])
    monkeypatch.setattr(books, 'reading_copy', read)
    result = book_legacy.reading_copy(scope, row['downloads'][0]['url'], 'txt')
    assert result['text'] == 'Contenido' and [c.args[1] for c in read.call_args_list] == ['txt', 'epub']
    read.reset_mock(side_effect=True); read.side_effect = requests.HTTPError(response=forbidden)
    with pytest.raises(requests.HTTPError): book_legacy.reading_copy(scope, row['downloads'][0]['url'], 'txt')
    assert read.call_count == 1
    read.reset_mock(side_effect=True); read.side_effect = books.BookError('protected_book')
    with pytest.raises(books.BookError, match='protected_book'): book_legacy.reading_copy(scope, row['downloads'][0]['url'], 'txt')
    assert read.call_count == 1
    read.reset_mock(side_effect=True); read.side_effect = requests.HTTPError(response=missing)
    with pytest.raises(requests.HTTPError): book_legacy.reading_copy(other, row['downloads'][0]['url'], 'txt')
    assert read.call_count == 1  # another instance's alternatives never apply


def test_internal_book_scope_comes_from_verified_binding_not_client_payload(monkeypatch):
    from michiflin import access, admin, state
    observed = []
    monkeypatch.setitem(miniapp.OPS, 'books', lambda chat_id, args, locale: observed.append(dict(args)) or {'books': []})
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, 'compatible_build', lambda *_args: True)
    monkeypatch.setattr(access, 'bound_allowed', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda *_args: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda *_args: True)
    monkeypatch.setattr(admin, 'seen', lambda *_args: None)
    monkeypatch.setattr(config, 'service_access_allowed', lambda *_args: True)
    monkeypatch.setattr(state, 'maintenance_active', lambda: False)
    monkeypatch.setattr(state, 'maintenance_interrupted', lambda *_args: False)
    monkeypatch.setattr(state, 'get_kill_switch', lambda *_args: False)
    monkeypatch.setattr(state, 'work_interrupted', lambda *_args: False)
    monkeypatch.setattr(state, 'guard_work', lambda *_args: contextlib.nullcontext())
    monkeypatch.setattr(miniapp, 'respond', mock.Mock())
    entry = {'chat': 23, 'addr': 'a@example.invalid', 'epoch': 'fixture', 'build': 'build', 'access_session': 'grant-1'}
    request = {'id': 'fixture-one', 'op': 'books', 'lang': 'es',
               'args': {'do': 'search', 'q': 'test', '_book_scope': 'attacker-controlled'}}
    miniapp.handle(17, entry, request)
    assert len(observed) == 1 and len(observed[0]['_book_scope']) == 64
    assert observed[0]['_book_scope'] != 'attacker-controlled'
    first = observed[0]['_book_scope']
    miniapp.handle(17, {**entry, 'access_session': 'grant-2'}, {**request, 'id': 'fixture-two'})
    assert observed[1]['_book_scope'] != first


def test_ambiguous_shared_file_never_falls_back_to_an_unrelated_record(monkeypatch):
    scope = '4' * 64
    first = {'downloads': [{'format': 'txt', 'url': 'https://example.org/shared.txt'},
                           {'format': 'epub', 'url': 'https://example.org/shared.epub'}]}
    second = {'downloads': [{'format': 'txt', 'url': 'https://example.org/shared.txt'},
                            {'format': 'epub', 'url': 'https://example.org/SHARED.epub'}]}
    book_legacy._remember_formats(scope, first)
    book_legacy._remember_formats(scope, second)
    response = requests.Response(); response.status_code = 404
    read = mock.Mock(side_effect=requests.HTTPError(response=response))
    monkeypatch.setattr(books, 'reading_copy', read)
    with pytest.raises(requests.HTTPError):
        book_legacy.reading_copy(scope, 'https://example.org/shared.txt', 'txt')
    read.assert_called_once()


def test_slow_combined_source_does_not_block_others_or_launch_an_automatic_retry(monkeypatch):
    monkeypatch.setattr(books, 'SOURCE_WAIT_SECONDS', 0.08)
    release, started, calls = threading.Event(), threading.Event(), []
    def fake(source, query, number, language, diagnostics, locale):
        calls.append((source, number))
        if source == 'google':
            started.set()
            assert release.wait(3)
        return [_book(source, number)], False
    monkeypatch.setattr(books, '_search', fake)
    scope = '5' * 64
    try:
        begin = time.monotonic()
        first = book_legacy.search(scope, 'all', 'slow-fixture', 1)
        assert time.monotonic() - begin < 1.5 and started.is_set()
        assert first['more'] and first['source_pages']['google'] == 1
        assert first['source_errors']['google'] == 'network_timeout'
        assert {book['source'] for book in first['books']} == {'gutenberg', 'openlibrary', 'archive', 'standard'}
        second = book_legacy.search(scope, 'all', 'slow-fixture', 2)
        assert second['more'] and not second['books']
        assert calls.count(('google', 1)) == 1  # the new click joins the already-started request
        with books._source_lock:
            pending = books._source_inflight[('google', 'slow-fixture', 1, '', 'es')]
        release.set()
        pending.result(timeout=2)
        third = book_legacy.search(scope, 'all', 'slow-fixture', 3)
        assert [row['id'] for row in third['books']] == ['google:1'] and not third['more']
        assert calls.count(('google', 1)) == 1
    finally:
        release.set()


def test_slow_single_source_keeps_page_and_reuses_the_inflight_request(monkeypatch):
    monkeypatch.setattr(books, 'SOURCE_WAIT_SECONDS', 0.08)
    release, calls = threading.Event(), []
    def fake(source, query, number, language, diagnostics, locale):
        calls.append(number)
        assert release.wait(3)
        return [_book(source, number)], False
    monkeypatch.setattr(books, '_search', fake)
    scope = '6' * 64
    try:
        first = book_legacy.search(scope, 'archive', 'slow-single', 1)
        second = book_legacy.search(scope, 'archive', 'slow-single', 2)
        assert first['more'] and second['more'] and 'tiempo de espera agotado' in first['unavailable'][0]
        assert calls == [1]
        with books._source_lock:
            pending = books._source_inflight[('archive', 'slow-single', 1, '', 'es')]
        release.set()
        pending.result(timeout=2)
        third = book_legacy.search(scope, 'archive', 'slow-single', 3)
        assert [row['id'] for row in third['books']] == ['archive:1'] and not third['more']
        assert calls == [1]
    finally:
        release.set()


def test_completed_catalogue_source_returns_capacity_before_inflight_cleanup(monkeypatch):
    started, finish, released = threading.Event(), threading.Event(), threading.Event()

    class ObservedSemaphore(threading.BoundedSemaphore):
        def release(self, n=1):
            super().release(n)
            released.set()

    def fake(source, query, number, language, diagnostics, locale):
        started.set()
        assert finish.wait(3)
        return [_book(source, number)], False

    monkeypatch.setattr(books, '_source_slots', ObservedSemaphore(1))
    monkeypatch.setattr(books, '_source_inflight', {})
    monkeypatch.setattr(books, '_search', fake)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(books, '_source_pool', pool)
        # A completed callback may need the same bookkeeping lock as the next
        # caller. Its finished work must not falsely exhaust the shared pool.
        with books._source_lock:
            first = books._submit_source('google', 'first', 1, '', 'es', False)
            assert first is not None and started.wait(2)
            finish.set()
            assert released.wait(2), 'completed work held the last slot while waiting for bookkeeping'
            second = books._submit_source('archive', 'second', 1, '', 'es', False)
            assert second is not None
        assert first.result(timeout=3)['books'][0]['source'] == 'google'
        assert second.result(timeout=3)['books'][0]['source'] == 'archive'


def test_saturated_catalogue_pool_fails_closed_without_queueing_more_work(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(books, '_source_slots', slots)
    fake = mock.Mock(return_value=([], False))
    monkeypatch.setattr(books, '_search', fake)
    try:
        result = book_legacy.search('7' * 64, 'archive', 'saturated-fixture', 1)
        assert result['more'] and 'ocupada temporalmente' in result['unavailable'][0]
        fake.assert_not_called()
    finally:
        slots.release()


def test_fallback_never_switches_to_a_different_file_family_in_the_same_record(monkeypatch):
    scope = '8' * 64
    row = {'downloads': [{'format': 'txt', 'url': 'https://example.org/volume-one.txt'},
                         {'format': 'epub', 'url': 'https://example.org/volume-two.epub'}]}
    book_legacy._remember_formats(scope, row)
    response = requests.Response(); response.status_code = 404
    read = mock.Mock(side_effect=requests.HTTPError(response=response))
    monkeypatch.setattr(books, 'reading_copy', read)
    with pytest.raises(requests.HTTPError):
        book_legacy.reading_copy(scope, row['downloads'][0]['url'], 'txt')
    read.assert_called_once()


def test_file_family_requires_same_directory_host_and_unambiguous_variant():
    assert books.same_file_family('https://archive.org/download/book/title_djvu.txt',
                                  'https://archive.org/download/book/title.epub')
    assert books.same_file_family('https://gutenberg.pglaf.org/cache/epub/108/pg108.txt',
                                  'https://gutenberg.pglaf.org/cache/epub/108/pg108-images-3.epub')
    assert not books.same_file_family('https://example.org/one/title.txt', 'https://example.org/two/title.epub')
    assert not books.same_file_family('https://example.org/title.txt', 'https://other.example/title.epub')
    assert not books.same_file_family('https://example.org/title.txt?edition=1', 'https://example.org/title.epub?edition=2')
