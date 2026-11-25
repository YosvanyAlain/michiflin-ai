"""End-to-end checks of the updated library UI and its isolated backend."""
from __future__ import annotations

import hashlib
from unittest import mock

import pytest
import requests

from michiflin import book_legacy, book_library, book_original, miniapp
from tests.test_webxdc_browser import browser_fixture


@pytest.fixture(autouse=True)
def isolate_book_runtime(monkeypatch):
    monkeypatch.setattr(book_library, '_cache', {})
    monkeypatch.setattr(book_library, '_source_pause', {})
    monkeypatch.setattr(book_library, '_source_pause_reason', {})
    monkeypatch.setattr(book_legacy, '_sessions', {})
    monkeypatch.setattr(book_legacy, '_alternatives', {})
    monkeypatch.setattr(book_original, '_sessions', {})


def _handler(request):
    # In production the authenticated dispatcher injects this scope, never the client.
    return miniapp.op_books(7, {**request['args'], '_book_scope': 'a' * 64}, request.get('lang', 'es'))


BRIDGE = '''() => {const original=bridge.call.bind(bridge);window.libraryRequests=[];
  bridge.call=async(op,args)=>{
    if(op!=='books')return original(op,args);
    window.libraryRequests.push(args);
    const response=await fetch('/api/delivery',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({op,args,lang:'es'})});const result=await response.json();
    if(result.error)throw new Error(result.error);return result.data;
  };
}'''


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_existing_ui_paginates_without_losing_a_failed_source(monkeypatch, engine):
    calls = []
    def fake(source, query, number, language, diagnostics, locale):
        calls.append((source, number))
        if source == 'google' and number == 1 and calls.count(('google', 1)) == 1:
            raise requests.Timeout()
        return [{'id': f'{source}:{number}', 'source': source, 'title': f'{source} {number}', 'authors': [],
                 'url': 'https://example.org/record',
                 'downloads': [{'format': 'txt', 'url': f'https://example.org/{source}{number}.txt'}]}], number == 1
    monkeypatch.setattr(book_library, '_search', fake)
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, delivery_handler=_handler) as (page, errors):
        page.evaluate(BRIDGE)
        page.evaluate("store.set('books.source','all');go('books',{q:'fixture'})")
        page.get_by_role('heading', name='gutenberg 1', exact=True).wait_for()
        assert 'Google Books' in page.get_by_role('status').inner_text()
        more = page.get_by_role('button', name='Más libros', exact=True)
        more.click()
        page.get_by_role('heading', name='google 1', exact=True).wait_for()
        page.get_by_role('heading', name='gutenberg 2', exact=True).wait_for()
        assert page.evaluate('window.libraryRequests.map(request=>request.page)') == [1, 2]
        more.click()
        page.get_by_role('heading', name='google 2', exact=True).wait_for()
        assert page.locator('.book-card').count() == 10 and not more.is_visible()
        assert calls.count(('google', 1)) == 2 and ('wikisource', 1) not in calls
        assert errors == []


def test_existing_ui_requires_explicit_format_after_failure_and_saved_books_survive_cache_clear(monkeypatch):
    calls = []
    raw = 'Libro completo de prueba.'.encode()
    def fake(source, query, number, language, diagnostics, locale):
        return [{'id': 'archive:fixture', 'source': 'archive', 'title': 'Libro descargado', 'authors': ['Autora'],
                 'url': 'https://example.org/record',
                 'downloads': [{'format': 'txt', 'url': 'https://example.org/book.txt'},
                               {'format': 'epub', 'url': 'https://example.org/book.epub'}]}], False
    def read(url, maximum, guard, **kwargs):
        assert guard is True and kwargs == {'anonymous': True}
        calls.append(url)
        if url.endswith('.epub'):
            response = requests.Response(); response.status_code = 404
            raise requests.HTTPError(response=response)
        return raw, 'text/plain'
    monkeypatch.setattr(book_library, '_search', fake)
    monkeypatch.setattr(book_library, '_fetch', read)
    with browser_fixture('es', {'width': 390, 'height': 844}, delivery_handler=_handler) as (page, errors):
        page.evaluate(BRIDGE)
        page.evaluate("store.set('books.source','archive');go('books',{q:'fixture'})")
        page.get_by_role('heading', name='Libro descargado', exact=True).wait_for()
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('status').get_by_text('no está disponible', exact=False).wait_for()
        assert calls == ['https://example.org/book.epub']  # never change a selected original silently
        page.get_by_label('Formato original').select_option('0')
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Guardado', exact=True).wait_for()
        assert calls[-1].endswith('.txt') and page.evaluate('(async()=> (await BookShelf.all()).length)()') == 1
        assert page.evaluate('(async()=> (await BookShelf.all())[0].original_sha256)()') == hashlib.sha256(raw).hexdigest()
        assert page.evaluate('(async()=> (await BookShelf.all())[0].text)()') == raw.decode()
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        assert page.locator('.book-reading').count() == 0
        page.evaluate('BookShelf.clearCache()')
        page.reload()
        page.wait_for_function('cache.at > 0')
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('heading', name='Libro descargado', exact=True).wait_for()
        assert page.evaluate("store.get('books.source')") == 'archive'
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 1 and errors == []


def test_existing_ui_does_not_report_html_or_unverified_web_link_as_a_book(monkeypatch):
    def fake(source, query, number, language, diagnostics, locale):
        return [{'id': 'web:html', 'source': 'web', 'title': 'Página parcial', 'authors': [],
                 'url': 'https://example.org/index', 'verification': 'candidate',
                 'downloads': [{'format': 'html', 'url': 'https://example.org/index'}]}], False
    monkeypatch.setattr(book_library, '_search', fake)
    with browser_fixture('es', {'width': 390, 'height': 844}, delivery_handler=_handler) as (page, errors):
        page.evaluate(BRIDGE)
        page.evaluate("store.set('books.source','web');go('books',{q:'fixture'})")
        page.get_by_role('status').get_by_text('formato verificable', exact=False).wait_for()
        assert page.get_by_role('heading', name='Página parcial', exact=True).count() == 0
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0 and errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_updated_ui_reassembles_and_saves_a_large_chunked_reading_copy(monkeypatch, engine):
    # Exercise the WebXDC bridge and the real Python chunker. The
    # sender, book and transport are synthetic, not a real Delta Chat account.
    text = 'Línea sintética 🎉\n' * 18000
    monkeypatch.setattr(book_library, '_search', lambda *_args: ([{
        'id': 'archive:long', 'source': 'archive', 'title': 'Libro extenso', 'authors': [],
        'url': 'https://example.org/record',
        'downloads': [{'format': 'txt', 'url': 'https://example.org/book.txt'}],
    }], False))
    monkeypatch.setattr(book_library, '_fetch', lambda *_args, **_kwargs: (text.encode(), 'text/plain'))
    monkeypatch.setattr(miniapp, '_answers', {})
    batches = []
    def handler(request):
        data = _handler(request)
        updates = []
        with mock.patch.object(miniapp, '_status_allowed', return_value=True), \
             mock.patch.object(miniapp, '_send_status', side_effect=lambda _id, payload: updates.append(payload) or True):
            miniapp.respond(7, request['id'], data)
        batches.append(updates)
        return {'updates': updates}
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, delivery_handler=handler) as (page, errors):
        page.evaluate('''() => {
          const original = window.webxdc.sendUpdate.bind(window.webxdc);
          window.webxdc.sendUpdate = (update, info) => {
            const request = update && update.payload;
            if (!request || request.t !== 'req' || request.op !== 'books') return original(update, info);
            fetch('/api/delivery', {method:'POST', headers:{'Content-Type':'application/json'},
              body:JSON.stringify(request)}).then(result => result.json()).then(body => {
              if (body.error) throw new Error(body.error);
              for (const payload of body.data.updates) window.__listener({serial:++window.__serial, payload});
            }).catch(error => { window.__chunkFixtureError = String(error); });
          };
          store.set('books.source','archive'); go('books',{q:'long-fixture'});
        }''')
        page.get_by_role('heading', name='Libro extenso', exact=True).wait_for()
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Guardado', exact=True).wait_for(timeout=30000)
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        assert page.evaluate("(async()=> (await BookShelf.all())[0].text === 'Línea sintética 🎉\\n'.repeat(18000))()")
        assert len(batches) >= 2 and len(batches[1]) > 1
        assert all(item['t'] == 'chunk' and len(item['s']) <= miniapp.CHUNK // 4 for item in batches[1])
        assert page.evaluate('(async()=> (await BookShelf.all())[0].original_sha256)()') == hashlib.sha256(text.encode()).hexdigest()
        assert not page.evaluate('window.__chunkFixtureError || false') and errors == []
