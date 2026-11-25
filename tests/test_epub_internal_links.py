"""Real taps and keyboard on synthetic EPUB links, with scripts disabled and no external network."""
from __future__ import annotations

import io
import zipfile

import pytest

from tests.test_book_originals import sample_epub
from tests.test_webxdc_browser import browser_fixture
from tests.test_welcome_reader_edges import import_one, show_reading_area, tap_edge


def linked_epub():
    first = ('<h1 id="inicio">Primer capítulo</h1>'
             '<p><a href="#destino%20uno" target="_top" ping="https://remote.invalid/ping" download="x">Saltar en este capítulo</a></p>'
             '<p><a href="../two.xhtml#destino-dos">Ir al segundo capítulo</a></p>'
             '<p>Texto seleccionable para continuar leyendo sin conexión.</p>'
             '<p><a href="https://remote.invalid/private?synthetic=secret" target="_top">Enlace externo</a></p>'
             '<p><a href="//remote.invalid/tracker">Enlace de red</a></p>'
             '<p><a href="javascript:parent.__book_attack=true">Enlace hostil</a></p>'
             '<p><a href="about:srcdoc#fake">Enlace falso</a></p>'
             '<p><a href="https://epub.invalid/two.xhtml#destino-dos">Origen falso</a></p>'
             '<p><a href="../two.xhtml?secret=synthetic">Consulta no admitida</a></p>'
             '<img src="https://remote.invalid/tracker"/><script>parent.__book_attack=true</script>'
             + ''.join(f'<p>Relleno {i}: una historia sintética para leer sin conexión.</p>' for i in range(45))
             + '<h2 id="destino uno">Destino del primer enlace</h2><p><a href="#inicio">Volver al comienzo</a></p>'
             + ''.join(f'<p>Final {i}: más texto de prueba para mantener páginas.</p>' for i in range(40)))
    raw = sample_epub(first)
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(result, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename in {'OEBPS/package.opf', 'OEBPS/nav.xhtml'}:
                data = data.replace(b'href="one.xhtml"', b'href="Text/one.xhtml"')
            elif info.filename == 'OEBPS/one.xhtml':
                info.filename = 'OEBPS/Text/one.xhtml'
            elif info.filename == 'OEBPS/two.xhtml':
                body = ('<h1 id="destino-dos">Destino del segundo capítulo</h1>'
                        '<p><a href="Text/one.xhtml#destino%20uno">Regresar al primero</a></p>'
                        '<p>Segundo capítulo sintético.</p>')
                data = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Dos</title>'
                        '</head><body>' + body + '</body></html>').encode()
            target.writestr(info, data)
    return result.getvalue()


def open_fixture(page):
    original_url = page.url
    book_id = import_one(page, 'enlaces.epub', 'application/epub+zip', linked_epub())
    page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
    show_reading_area(page, '.book-file-stage')
    assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
    return book_id, original_url


def target_in_stage(page, target):
    return page.evaluate('''id => {
      const stage=document.querySelector('.book-file-stage'), frame=stage?.querySelector('iframe');
      const element=frame?.contentDocument?.getElementById(id); if(!element) return false;
      const s=stage.getBoundingClientRect(), f=frame.getBoundingClientRect(), r=element.getBoundingClientRect();
      return f.left+r.right > s.left && f.left+r.left < s.right && f.top+r.bottom > s.top && f.top+r.top < s.bottom;
    }''', target)


def wait_for_target(page, target):
    page.wait_for_function('''id => {
      const stage=document.querySelector('.book-file-stage'), frame=stage?.querySelector('iframe');
      const element=frame?.contentDocument?.getElementById(id); if(!element) return false;
      const s=stage.getBoundingClientRect(), f=frame.getBoundingClientRect(), r=element.getBoundingClientRect();
      return f.left+r.right > s.left && f.left+r.left < s.right && f.top+r.bottom > s.top && f.top+r.top < s.bottom;
    }''', arg=target, timeout=15000)


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_real_touch_opens_percent_encoded_fragment_and_persists_position(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id, original_url = open_fixture(page)
        frame = page.frame_locator('.book-file-stage iframe').first
        label = page.locator('.book-file-page').inner_text()
        original_position = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        network = []
        page.on('request', lambda request: network.append(request.url) if request.url.startswith(('http://', 'https://')) else None)
        assert not target_in_stage(page, 'destino uno')
        frame.get_by_text('Saltar en este capítulo', exact=True).tap()
        wait_for_target(page, 'destino uno')
        page.wait_for_function("old => document.querySelector('.book-file-page').textContent !== old", arg=label, timeout=15000)
        assert page.url == original_url
        page.wait_for_function("({id,old}) => store.get('book.filePosition.'+id) !== old", arg={'id': book_id, 'old': original_position})
        assert network == []  # no HTTP request, not even to the local app origin
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        wait_for_target(page, 'destino uno')
        assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_repeated_same_link_resets_signal_and_keeps_native_link_focus(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        _book_id, original_url = open_fixture(page)
        frame = page.frame_locator('.book-file-stage iframe').first
        first = frame.get_by_text('Saltar en este capítulo', exact=True)
        initial_history = page.evaluate('history.length')
        assert first.evaluate('node => node.onclick === null && node.href.startsWith("about:srcdoc#")')
        first.tap()
        wait_for_target(page, 'destino uno')
        page.wait_for_function("document.querySelector('.book-file-stage iframe').contentWindow.location.href === 'about:srcdoc'")
        frame.get_by_text('Volver al comienzo', exact=True).tap()
        wait_for_target(page, 'inicio')
        page.wait_for_function("document.querySelector('.book-file-stage iframe').contentWindow.location.href === 'about:srcdoc'")
        first.focus()
        page.keyboard.press('Enter')
        wait_for_target(page, 'destino uno')
        # Native fragment navigation must not add one dead Back entry per tap.
        assert page.evaluate('history.length') <= initial_history + 1
        assert page.url == original_url and errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_keyboard_navigates_relative_chapters_and_returns_to_fragment(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        _book_id, original_url = open_fixture(page)
        frame = page.frame_locator('.book-file-stage iframe').first
        frame.get_by_text('Ir al segundo capítulo', exact=True).focus()
        page.wait_for_timeout(250)  # Tab/focus alone must not activate the link.
        assert 'Sección 1 / 2' in page.locator('.book-file-page').inner_text()
        assert page.evaluate("document.querySelector('.book-file-stage iframe').contentWindow.location.href") == 'about:srcdoc'
        page.keyboard.press('Enter')
        page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('Sección 2 / 2')", timeout=15000)
        wait_for_target(page, 'destino-dos')
        frame = page.frame_locator('.book-file-stage iframe').first
        frame.get_by_text('Regresar al primero', exact=True).focus()
        page.keyboard.press('Enter')
        page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('Sección 1 / 2')", timeout=15000)
        wait_for_target(page, 'destino uno')
        assert page.url == original_url
        assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_real_text_selection_is_not_covered_or_turned_by_gutter(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        _book_id, _original_url = open_fixture(page)
        frame = page.frame_locator('.book-file-stage iframe').first
        paragraph = frame.get_by_text('Texto seleccionable para continuar leyendo sin conexión.', exact=True)
        box = paragraph.bounding_box()
        assert box is not None
        x, y = box['x'] + 30, box['y'] + min(12, box['height'] / 2)
        assert page.evaluate('({x,y}) => document.elementFromPoint(x,y).tagName', {'x': x, 'y': y}) == 'IFRAME'
        if engine == 'webkit':
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + 60, y, steps=4)
            page.mouse.up()
        else:
            # In this headless environment dragging within even a bare
            # sandboxed srcdoc can hang Playwright; double-click still makes
            # a genuine native selection without changing the sandbox.
            page.mouse.dblclick(x, y, delay=50)
        assert paragraph.evaluate('() => window.getSelection().toString().length') > 0
        before = page.locator('.book-file-page').inner_text()
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_timeout(350)
        assert page.locator('.book-file-page').inner_text() == before and errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_hostile_and_external_links_cannot_execute_or_reach_network(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        remote = []
        def blocked(route):
            remote.append(route.request.url)
            route.abort()
        page.route('**://remote.invalid/**', blocked)
        _book_id, original_url = open_fixture(page)
        frame = page.frame_locator('.book-file-stage iframe').first
        for text in ['Enlace externo', 'Enlace de red', 'Enlace hostil', 'Enlace falso', 'Origen falso', 'Consulta no admitida']:
            assert frame.get_by_text(text, exact=True).get_attribute('href') is None
        first = frame.get_by_text('Saltar en este capítulo', exact=True)
        assert all(first.get_attribute(name) is None for name in ('target', 'ping', 'download'))
        assert frame.locator('script, iframe, object, form').count() == 0
        assert not page.evaluate('window.__book_attack || false')
        assert remote == [] and page.url == original_url and errors == []
