"""Reader regressions with synthetic content: no mail, user accounts or network."""
from __future__ import annotations

import base64
import html
import io
import json
import time

from bs4 import BeautifulSoup
from PIL import Image
import pytest

from michiflin import access, browser, chat, miniapp, net, reader, state, web, web_vault


class Response:
    def __init__(self, body: bytes, url: str, content_type: str = 'text/html'):
        self.body = body
        self.url = url
        self.headers = {'content-type': content_type}
        self.ok = True
        self.status_code = 200
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    previous = chat.sender()
    monkeypatch.setattr(reader, '_cache', {})
    monkeypatch.setattr(reader, '_cache_versions', {})
    monkeypatch.setattr(reader, '_site_pauses', {})
    monkeypatch.setattr(net, '_jars', {})
    monkeypatch.setattr(browser, 'available', lambda: False)
    yield
    chat.set_sender(previous)


@pytest.fixture
def settings(monkeypatch):
    values = {'reader_images': 'off', 'reader_links': 'web', 'reader_kb': 30}
    monkeypatch.setattr(state, 'setting', lambda key: values[key])
    return values


def image_bytes(size=(160, 100)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (30, 120, 200)).save(buffer, format='PNG')
    return buffer.getvalue()


def test_reader_cache_is_private_per_sender_and_uses_redirect_alias(monkeypatch, settings):
    original = 'https://fixture.example/article'
    final = 'https://fixture.example/final'
    requested = []
    responses = []

    def fetch(url, **kwargs):
        assert url == original and kwargs['stream'] is True
        name = chat.sender().split('@')[0].upper()
        markup = ('<title>Fixture</title><article><p>' + 'x' * 30000 + '</p><p>'
                  + name + '_PRIVATE_CONTENT ' + 'y' * 3000 + '</p></article>')
        response = Response(markup.encode(), final)
        requested.append(name)
        responses.append(response)
        return response

    monkeypatch.setattr(net, 'fetch', fetch)
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)
    chat.set_sender('alice@example.invalid')
    first = miniapp.op_read(1, {'url': original, 'page': 1}, 'es')
    assert first['pages'] == 2
    chat.set_sender('bob@example.invalid')
    bob = miniapp.op_read(2, {'url': original, 'page': 2}, 'es')
    assert 'BOB_PRIVATE_CONTENT' in bob['html'] and 'ALICE_PRIVATE_CONTENT' not in bob['html']
    chat.set_sender('alice@example.invalid')
    alice = miniapp.op_read(1, {'url': original, 'page': 2}, 'es')
    assert 'ALICE_PRIVATE_CONTENT' in alice['html'] and 'BOB_PRIVATE_CONTENT' not in alice['html']
    assert reader.cached(original) is reader.cached(final)
    assert requested == ['ALICE', 'BOB'] and all(response.closed for response in responses)


def test_cache_fails_closed_for_missing_or_mismatched_sender_and_changed_preferences(settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.invalid')
    page = reader.Page(url, blocks=['<p>Private Alice</p>'])
    reader._remember(page, url, reader._cache_key(url))
    assert reader.cached(url) is page
    with state.person('bob@example.invalid'):
        assert reader.cached(url) is None  # chat cookie identity and settings identity disagree
    settings['reader_images'] = 'low'
    assert reader.cached(url) is None
    settings['reader_images'] = 'off'
    settings['reader_links'] = 'chat'
    assert reader.cached(url) is None
    chat.set_sender('')
    assert reader.cached(url) is None
    reader._remember(reader.Page(url), url, None)
    assert all(key[0] == 'alice@example.invalid' for key in reader._cache)


def test_cache_has_lru_and_size_limits_and_expires(monkeypatch, settings):
    chat.set_sender('alice@example.invalid')
    monkeypatch.setattr(reader, 'CACHE_ENTRIES', 2)
    pages = [reader.Page(f'https://fixture.example/{n}', text=f'page {n}') for n in range(3)]
    for page in pages[:2]:
        reader._remember(page, page.url, reader._cache_key(page.url))
    assert reader.cached(pages[0].url) is pages[0]
    reader._remember(pages[2], pages[2].url, reader._cache_key(pages[2].url))
    assert reader.cached(pages[1].url) is None
    assert reader.cached(pages[0].url) is pages[0] and reader.cached(pages[2].url) is pages[2]
    pages[0].created = time.time() - reader.CACHE_SECONDS - 1
    assert reader.cached(pages[0].url) is None
    monkeypatch.setattr(reader, 'CACHE_BYTES', 100)
    large = reader.Page('https://fixture.example/large', text='x' * 1000)
    reader._remember(large, large.url, reader._cache_key(large.url))
    assert reader.cached(large.url) is None


def test_picture_lazy_and_standalone_images_preserve_article_and_never_send_remote_sources(monkeypatch, settings):
    settings['reader_images'] = 'low'
    fetched, responses = [], []

    def fetch(url, **kwargs):
        assert kwargs['anonymous'] is True and kwargs['stream'] is True
        fetched.append(url)
        response = Response(image_bytes(), url, 'image/png')
        responses.append(response)
        return response

    monkeypatch.setattr(net, 'fetch', fetch)
    markup = '''<html><head><title>Historia</title><script>bad()</script></head><body>
      <header><img src="/tracking-logo.png"><nav><a href="/">Inicio</a></nav></header>
      <article><header><h1>Historia</h1><p>Por Autora · 2026</p></header>
      <p>Una introducción útil y legible.</p><figure><picture>
      <source srcset="/cover.webp 1x, /cover2.webp 2x"><img src="data:image/gif;base64,AA==" alt="Portada">
      </picture><figcaption>El pie de la fotografía</figcaption></figure>
      <img src="/pixel.gif" data-src="/lazy.png" alt="Imagen independiente">
      <p>''' + 'Contenido del artículo. ' * 25 + '''<a href="/more">Leer más</a>
      <a href="javascript:alert(1)">Malicioso</a></p><svg onload="bad()"></svg></article></body></html>'''
    page = reader.parse('https://fixture.example/story', markup)
    rendered = ''.join(page.blocks)
    soup = BeautifulSoup(rendered, 'html.parser')
    images = soup.find_all('img')
    assert fetched == ['https://fixture.example/cover.webp', 'https://fixture.example/lazy.png']
    assert all(response.closed for response in responses)
    assert len(images) == 2 and all(image['src'].startswith('data:image/jpeg;base64,') for image in images)
    assert all(len(base64.b64decode(image['src'].split(',', 1)[1])) <= reader.IMAGE_OUTPUT_BYTES for image in images)
    assert soup.figure and soup.figure.img and soup.figure.figcaption.get_text() == 'El pie de la fotografía'
    assert 'Por Autora' in soup.get_text() and page.title == 'Historia'
    assert not soup.find('h1')  # the unchanged app and chat HTML already show the same title
    assert soup.find('a', string='Leer más')['href'] == 'https://fixture.example/more'
    assert not soup.find('a', string='Malicioso').has_attr('href')
    assert not soup.find('script') and not soup.find('svg') and 'onload' not in rendered
    assert '/tracking-logo.png' not in fetched and 'https://fixture.example/lazy.png' not in rendered


def test_different_or_linked_heading_is_not_silently_removed(settings):
    different = reader.parse('https://fixture.example/a', '<title>Publicación</title><article><h1>Otro título</h1><p>Texto</p></article>')
    linked = reader.parse('https://fixture.example/a', '<title>Título</title><article><h1><a href="/home">Título</a></h1><p>Texto</p></article>')
    assert 'Otro título' in ''.join(different.blocks)
    assert '<h1>' in ''.join(linked.blocks) and 'href=' in ''.join(linked.blocks)
    title_only = reader.parse('https://fixture.example/a', '<title>Único</title><h1>Único</h1>')
    assert title_only.text == 'Único' and 'Único' in ''.join(title_only.blocks)


def test_images_off_makes_no_image_request_and_keeps_alternative_text(monkeypatch, settings):
    monkeypatch.setattr(reader, '_tiny_image', lambda *_args: pytest.fail('image opt-out ignored'))
    page = reader.parse('https://fixture.example/a', '<article><figure><picture><source srcset="/p.webp">'
                        '<img src="/p.jpg" alt="Descripción"></picture><figcaption>Pie</figcaption></figure>'
                        '<p>Texto.</p></article>')
    markup = ''.join(page.blocks)
    assert '<img' not in markup and '[Descripción]' in markup and 'Pie' in markup


def test_failed_image_attempts_are_bounded_and_only_content_is_fetched(monkeypatch, settings):
    settings['reader_images'] = 'low'
    sources = []
    monkeypatch.setattr(reader, '_tiny_image', lambda url: sources.append(url) or '')
    outside = '<header><img src="/logo.png"></header>'
    inside = ''.join(f'<img src="/image{n}.png" alt="Imagen {n}">' for n in range(40))
    markup = f'<body>{outside}<article><p>{"Main content. " * 60}</p>{inside}</article></body>'
    page = reader.parse('https://fixture.example/article', markup)
    assert sources == [f'https://fixture.example/image{n}.png' for n in range(reader.IMAGE_ATTEMPTS)]
    assert '[Imagen 39]' in ''.join(page.blocks)


def test_image_response_is_closed_on_success_bad_type_size_and_pixel_limit(monkeypatch):
    responses = []
    bodies = [(image_bytes(), 'image/png'), (b'bad', 'text/html'),
              (b'x' * (reader.IMAGE_INPUT_BYTES + 1), 'image/png'),
              (image_bytes((3000, 3000)), 'image/png')]

    def fetch(url, **kwargs):
        assert kwargs['anonymous'] is True
        body, kind = bodies[len(responses)]
        response = Response(body, url, kind)
        responses.append(response)
        return response

    monkeypatch.setattr(net, 'fetch', fetch)
    assert reader._tiny_image('https://fixture.example/a').startswith('data:image/jpeg;base64,')
    assert reader._tiny_image('https://fixture.example/b') == ''
    assert reader._tiny_image('https://fixture.example/c') == ''
    assert reader._tiny_image('https://fixture.example/d') == ''
    assert len(responses) == 4 and all(response.closed for response in responses)


def test_image_private_address_is_rejected_before_network():
    assert reader._tiny_image('http://127.0.0.1/private.png') == ''


def test_nested_structures_and_standalone_images_remain_complete(settings, monkeypatch):
    settings['reader_images'] = 'low'
    monkeypatch.setattr(reader, '_tiny_image', lambda _: 'data:image/jpeg;base64,AA==')
    markup = ('<article><p>Antes</p><blockquote><p>Cita anidada</p></blockquote>'
              '<figure><img src="/p.jpg" alt="p"><figcaption><p>Pie anidado</p></figcaption></figure>'
              '<table><tbody><tr><td><p>Celda</p></td></tr></tbody></table>'
              '<img src="/b.jpg" alt="independiente"><p>Después</p></article>')
    page = reader.parse('https://fixture.example/a', markup)
    fragments = [BeautifulSoup(block, 'html.parser') for block in page.blocks]
    assert any(fragment.blockquote and fragment.blockquote.p for fragment in fragments)
    assert any(fragment.figure and fragment.figure.img and fragment.figure.figcaption.p for fragment in fragments)
    assert any(fragment.table and fragment.table.td.p for fragment in fragments)
    assert sum(len(fragment.find_all('img')) for fragment in fragments) == 2
    assert fragments[-1].get_text() == 'Después'


def test_oversized_table_is_paginated_as_balanced_bounded_html_without_losing_text(settings):
    content = 'palabra á & < > 🎉 ' * 8000
    markup = ('<table><thead><tr><th>Nombre</th></tr></thead><tbody><tr><td><b>'
              + html.escape(content) + '</b></td></tr></tbody></table>')
    chunks = reader.paginate(reader.Page('https://fixture.example/a', blocks=[markup]), 30)
    assert len(chunks) > 1 and all(len('\n'.join(chunk).encode()) <= 30 * 1024 for chunk in chunks)
    parsed = [BeautifulSoup('\n'.join(chunk), 'html.parser') for chunk in chunks]
    assert all(fragment.table and fragment.table.td and fragment.table.td.b for fragment in parsed)
    assert ''.join(fragment.get_text() for fragment in parsed) == 'Nombre' + content


def test_oversized_link_and_unicode_keep_text_and_target(settings):
    content = 'Árbol & café 🎉 ' * 9000
    block = '<p><a href="https://fixture.example/a?x=1&amp;y=2">' + html.escape(content) + '</a></p>'
    chunks = reader.paginate(reader.Page('https://fixture.example/a', blocks=[block]), 30)
    parsed = [BeautifulSoup('\n'.join(chunk), 'html.parser') for chunk in chunks]
    assert len(chunks) > 1 and all(len('\n'.join(chunk).encode()) <= 30 * 1024 for chunk in chunks)
    assert all(fragment.a['href'] == 'https://fixture.example/a?x=1&y=2' for fragment in parsed)
    assert ''.join(fragment.get_text() for fragment in parsed) == content


def test_chat_keeps_html_and_plain_summary_without_claiming_native_presentation(monkeypatch, settings):
    page = reader.Page('https://fixture.example/story', title='Historia',
                       blocks=['<p>Texto de muestra.</p>', '<figure><img src="data:image/jpeg;base64,AA==" alt="Portada"><figcaption>Pie</figcaption></figure>'])
    monkeypatch.setattr(reader, 'load', lambda _url: page)
    monkeypatch.setattr(state, 'link_id', lambda _url: 'fixture')
    monkeypatch.setattr(web.media, 'available', lambda: False)
    sent = []
    monkeypatch.setattr(chat, 'send', lambda _chat, text, **kwargs: sent.append((text, kwargs['html'])))
    web.read(1, page.url, 'es')
    summary, document = sent[0]
    assert 'Texto de muestra' in summary and '/pdf_fixture' in summary
    assert '<figure>' in document and '<figcaption>Pie</figcaption>' in document and '<style>' in document


def test_cache_rotates_when_contact_cookies_change_expire_or_are_restored(settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    jar = net.cookie_jar()
    anonymous = reader.Page(url, text='public')
    reader._remember(anonymous, url, reader._cache_key(url))
    assert reader.cached(url) is anonymous
    jar.set('session', 'synthetic-private-value', domain='fixture.example', path='/', expires=int(time.time()) + 3600)
    assert reader.cached(url) is None
    private = reader.Page(url, text='private')
    reader._remember(private, url, reader._cache_key(url))
    assert reader.cached(url) is private
    assert 'synthetic-private-value' not in repr(list(reader._cache))
    cookie = next(cookie for cookie in jar if cookie.name == 'session')
    cookie.expires = int(time.time()) - 1
    assert reader.cached(url) is None
    cookie.expires = int(time.time()) + 3600
    assert reader.cached(url) is None  # old private entry cannot resurface
    reader._remember(private, url, reader._cache_key(url))
    jar.clear(domain='fixture.example', path='/', name='session')
    assert reader.cached(url) is None
    assert all(page is not private for page in reader._cache.values())


def test_cookie_change_during_fetch_prevents_cache_insertion(monkeypatch, settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    def fetch(_url, **_kwargs):
        net.cookie_jar().set('session', 'new-synthetic-cookie', domain='fixture.example', path='/')
        return Response(b'<title>Title</title><p>Private content</p>', url)
    monkeypatch.setattr(net, 'fetch', fetch)
    page = reader.load(url)
    assert page.text == 'Private content' and reader.cached(url) is None
    assert not reader._cache


def test_cookie_mutation_and_restore_during_fetch_cannot_cache_intermediate_content(monkeypatch, settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    jar = net.cookie_jar()
    jar.set('session', 'synthetic-public', domain='fixture.example', path='/')
    def fetch(_url, **_kwargs):
        jar.set('session', 'synthetic-private', domain='fixture.example', path='/')
        body = b'<title>Private</title><p>Intermediate session content</p>'
        jar.set('session', 'synthetic-public', domain='fixture.example', path='/')
        return Response(body, url)
    monkeypatch.setattr(net, 'fetch', fetch)
    page = reader.load(url)
    assert page.text == 'Intermediate session content'
    assert reader.cached(url) is None and not reader._cache


def test_concurrent_cookie_round_trip_invalidates_inflight_page(monkeypatch, settings):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    jar = net.cookie_jar()
    jar.set('session', 'synthetic-public', domain='fixture.example', path='/')
    started, resume = threading.Event(), threading.Event()
    def fetch(_url, **_kwargs):
        started.set()
        assert resume.wait(5), 'test did not release the in-flight fetch'
        return Response(b'<p>Intermediate private content</p>', url)
    def load_on_request_thread():
        chat.set_sender('alice@example.org')
        return reader.load(url)
    monkeypatch.setattr(net, 'fetch', fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(load_on_request_thread)
        try:
            assert started.wait(5)
            jar.set('session', 'synthetic-private', domain='fixture.example', path='/')
            jar.set('session', 'synthetic-public', domain='fixture.example', path='/')
        finally:
            resume.set()
        assert future.result(timeout=5).text == 'Intermediate private content'
    assert reader.cached(url) is None and not reader._cache


def test_evicted_cookie_jar_cannot_survive_in_a_thread_local_session(settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    original = net.session()
    original.cookies.set('session', 'synthetic-private', domain='fixture.example', path='/')
    key = reader._cache_key(url)
    reader._remember(reader.Page(url, text='original private content'), url, key)
    for index in range(260):
        chat.set_sender(f'person{index}@example.org')
        net.cookie_jar()
    chat.set_sender('alice@example.org')
    replacement = net.cookie_jar()
    assert replacement is not original.cookies
    assert net.session().cookies is replacement
    assert not any(cookie.name == 'session' for cookie in replacement)
    assert reader.cached(url) is None
    reader._remember(reader.Page(url, text='stale'), url, key)
    assert reader.cached(url) is None


def test_recreated_identical_cookie_jar_has_distinct_cache_generation(settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    first_key = reader._cache_key(url)
    first_jar = net.cookie_jar()
    for index in range(260):
        chat.set_sender(f'person{index}@example.org')
        net.cookie_jar()
    chat.set_sender('alice@example.org')
    assert net.cookie_jar() is not first_jar
    assert reader._cache_key(url)[4] != first_key[4]


def test_saved_browser_session_change_invalidates_only_owner_and_inflight_fetch(monkeypatch, settings, tmp_path):
    url = 'https://fixture.example/article'
    monkeypatch.setattr(web_vault.config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setenv('WEB_SESSION_KEY', 'synthetic-key-used-only-in-test-12345')
    alice, bob = 'alice@example.org', 'bob@example.org'
    chat.set_sender(alice)
    alice_key = reader._cache_key(url)
    alice_page = reader.Page(url, text='alice')
    reader._remember(alice_page, url, alice_key)
    chat.set_sender(bob)
    bob_page = reader.Page(url, text='bob')
    reader._remember(bob_page, url, reader._cache_key(url))
    web_vault.save(alice, 'host:fixture.example', {'cookies': [], 'origins': []})
    assert reader.cached(url) is bob_page
    chat.set_sender(alice)
    assert reader.cached(url) is None
    reader._remember(alice_page, url, alice_key)  # request started before invalidation
    assert reader.cached(url) is None
    reader._remember(alice_page, url, reader._cache_key(url))
    assert reader.cached(url) is alice_page
    assert web_vault.remove(alice, 'host:fixture.example')
    assert reader.cached(url) is None
    chat.set_sender(bob)
    assert reader.cached(url) is bob_page


def test_bounded_principal_versions_prevent_old_inflight_reinsertion(settings):
    url = 'https://fixture.example/article'
    chat.set_sender('alice@example.org')
    old = reader._cache_key(url)
    for index in range(260):
        chat.set_sender(f'person{index}@example.org')
        assert reader._cache_key(url) is not None
    assert len(reader._cache_versions) <= 256
    chat.set_sender('alice@example.org')
    reader._remember(reader.Page(url, text='obsolete'), url, old)
    assert reader.cached(url) is None


@pytest.mark.parametrize('status', [401, 403, 429, 451, 503])
def test_blocked_pages_never_retry_through_proxy_or_browser(monkeypatch, settings, status):
    url = 'https://fixture.example/article'
    calls, responses = [], []
    monkeypatch.setattr(net, 'WEB_PROXY', 'http://configured-proxy.invalid')
    monkeypatch.setattr(browser, 'available', lambda: True)
    monkeypatch.setattr(browser, 'render', lambda _url: pytest.fail('must not render a blocked page'))
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy))
        response = Response(b'<title>Blocked</title>', target)
        response.status_code = status
        response.ok = False
        responses.append(response)
        return response
    monkeypatch.setattr(net, 'request', request)
    with pytest.raises(ValueError, match=f'http_{status}'):
        reader.load(url)
    assert calls == [('GET', url, False)] and all(response.closed for response in responses)
    if status == 429:
        with pytest.raises(ValueError, match='rate_limited'):
            reader.load(url)
        assert len(calls) == 1
        assert 'Espera' in web.url_error(ValueError('rate_limited'), 'es')


def test_http_200_verification_stops_before_images_or_browser_and_pauses(monkeypatch, settings):
    url = 'https://fixture.example/challenge'
    settings['reader_images'] = 'low'
    calls = []
    monkeypatch.setattr(browser, 'available', lambda: True)
    monkeypatch.setattr(browser, 'render', lambda _url: pytest.fail('must not retry verification in a browser'))
    def fetch(target, **_kwargs):
        calls.append(target)
        return Response(b'<title>Just a moment...</title><p>Checking your browser</p><img src="/challenge.png">', target)
    monkeypatch.setattr(net, 'fetch', fetch)
    with pytest.raises(ValueError, match='challenge'):
        reader.load(url)
    with pytest.raises(ValueError, match='rate_limited'):
        reader.load(url)
    assert calls == [url] and 'verificación' in web.url_error(ValueError('challenge'), 'es')


def test_rendered_verification_stops_without_parsing_thumbnails(monkeypatch, settings):
    url = 'https://fixture.example/article'
    settings['reader_images'] = 'low'
    monkeypatch.setattr(net, 'fetch', lambda target, **_kw: Response(b'<p>Enable javascript</p>', target))
    monkeypatch.setattr(browser, 'available', lambda: True)
    monkeypatch.setattr(browser, 'render', lambda _url: (url, '<title>Just a moment...</title><img src="/captcha.png">'))
    monkeypatch.setattr(reader, '_tiny_image', lambda _url: pytest.fail('challenge must not fetch images'))
    with pytest.raises(ValueError, match='challenge'):
        reader.load(url)
    assert reader._site_paused(url)


def test_image_limit_stops_remaining_image_attempts_without_proxy_hopping(monkeypatch, settings):
    settings['reader_images'] = 'low'
    monkeypatch.setattr(net, 'WEB_PROXY', 'http://configured-proxy.invalid')
    calls, responses = [], []
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy, kwargs.get('anonymous')))
        response = Response(b'', target, 'image/png')
        response.status_code, response.ok = 429, False
        responses.append(response)
        return response
    monkeypatch.setattr(net, 'request', request)
    markup = '<article><p>' + 'content ' * 80 + '</p>' + ''.join(
        f'<img src="/pic{n}.png" alt="Alt {n}">' for n in range(5)) + '</article>'
    page = reader.parse('https://fixture.example/article', markup)
    assert calls == [('GET', 'https://fixture.example/pic0.png', False, True)]
    assert all(response.closed for response in responses) and '[Alt 4]' in ''.join(page.blocks)
    assert reader._site_paused('https://fixture.example/other')


def test_browser_fallback_shares_single_image_budget(monkeypatch, settings):
    url = 'https://fixture.example/article'
    settings['reader_images'] = 'low'
    first = '<article><p>Short.</p>' + ''.join(f'<img src="/first{n}.png">' for n in range(3)) + '</article>'
    rendered = '<article><p>' + 'Useful content. ' * 100 + '</p>' + ''.join(
        f'<img src="/second{n}.png">' for n in range(8)) + '</article>'
    monkeypatch.setattr(net, 'fetch', lambda target, **_kw: Response(first.encode(), target))
    monkeypatch.setattr(browser, 'available', lambda: True)
    monkeypatch.setattr(browser, 'render', lambda _url: (url, rendered))
    attempted = []
    monkeypatch.setattr(reader, '_tiny_image', lambda source: attempted.append(source) or '')
    page = reader.load(url)
    assert len(page.text) > 500 and len(attempted) == reader.IMAGE_ATTEMPTS
    assert attempted == [f'https://fixture.example/first{n}.png' for n in range(3)] + ['https://fixture.example/second0.png']


def test_oversized_json_envelope_is_chunked_and_reassembles_without_loss(monkeypatch, settings):
    settings['reader_kb'] = 70
    text = '"\\ Á🙂 & ' * 18000
    page = reader.Page('https://fixture.example/article', title='Título',
                       blocks=['<p>' + html.escape(text, quote=False) + '</p>'],
                       nav=[('Sección', f'https://fixture.example/{n}/' + 'x' * 4000) for n in range(20)])
    data = reader.app_page(page, 1)
    assert len(data['html'].encode('utf-8')) <= 70 * 1024
    updates = []
    monkeypatch.setattr(miniapp, '_answers', {})
    monkeypatch.setattr(miniapp, '_status_allowed', lambda _instance: True)
    monkeypatch.setattr(miniapp, '_send_status', lambda _instance, payload: updates.append(payload) or True)
    miniapp.respond(7, 'synthetic-rid', data)
    assert len(updates) > 1 and all(item['t'] == 'chunk' for item in updates)
    assert [item['i'] for item in updates] == list(range(len(updates)))
    assert all(item['n'] == len(updates) for item in updates)
    assert all(len(json.dumps({'payload': item}, ensure_ascii=False, separators=(',', ':')).encode()) < 128000 for item in updates)
    decoded = json.loads(''.join(item['s'] for item in updates))
    assert decoded['ok'] and decoded['data'] == data


def test_new_access_grant_gets_new_cookie_jar_and_cannot_reuse_old_page(monkeypatch, settings):
    url = 'https://fixture.example/article'
    current = ['synthetic-grant-one']
    monkeypatch.setattr(access, 'session_stamp', lambda _address: current[0])
    chat.set_sender('alice@example.org')
    old_session = net.session()
    old_session.cookies.set('session', 'synthetic-secret', domain='fixture.example', path='/')
    old_page = reader.Page(url, text='private to first grant')
    reader._remember(old_page, url, reader._cache_key(url))
    assert reader.cached(url) is old_page
    current[0] = 'synthetic-grant-two'
    new_session = net.session()
    assert new_session is not old_session and new_session.cookies is not old_session.cookies
    assert not any(cookie.name == 'session' for cookie in new_session.cookies)
    assert reader.cached(url) is None
    assert all(page is not old_page for page in reader._cache.values())


def test_unknown_grant_never_reuses_private_cookie_jar_or_cached_page(monkeypatch, settings):
    chat.set_sender('alice@example.org')
    def unknown(_address):
        raise RuntimeError('synthetic access registry failure')
    monkeypatch.setattr(access, 'session_stamp', unknown)
    first = net.cookie_jar()
    first.set('session', 'synthetic-secret', domain='fixture.example', path='/')
    assert net.cookie_jar() is not first
    url = 'https://fixture.example/article'
    reader._remember(reader.Page(url, text='private'), url, reader._cache_key(url))
    assert reader.cached(url) is None


def test_cache_never_takes_state_lock_under_cache_lock(monkeypatch, settings):
    original, original_grant = state.setting, access.session_stamp
    def checked(key):
        assert not reader._cache_lock._is_owned(), 'lock order would deadlock against session save'
        return original(key)
    def checked_grant(address):
        assert not reader._cache_lock._is_owned(), 'grant lookup would deadlock against session save'
        return original_grant(address)
    monkeypatch.setattr(state, 'setting', checked)
    monkeypatch.setattr(access, 'session_stamp', checked_grant)
    chat.set_sender('alice@example.org')
    url = 'https://fixture.example/article'
    page = reader.Page(url, text='safe')
    reader._remember(page, url, reader._cache_key(url))
    assert reader.cached(url) is page


def test_invalidation_between_cache_snapshot_and_insert_wins(monkeypatch, settings):
    chat.set_sender('alice@example.org')
    url = 'https://fixture.example/article'
    original = reader._cache_key
    key = original(url)
    def racing(target):
        value = original(target)
        reader.invalidate_sender('alice@example.org')
        return value
    monkeypatch.setattr(reader, '_cache_key', racing)
    reader._remember(reader.Page(url, text='obsolete'), url, key)
    assert not reader._cache
