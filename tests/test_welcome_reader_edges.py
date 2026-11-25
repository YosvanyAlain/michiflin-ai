"""Welcome countdown and offline page-edge navigation with synthetic fixtures.

These tests use the simulated WebXDC bridge, not a real account or mail delivery.
"""
from __future__ import annotations

import re

import pytest

from tests.test_book_originals import sample_epub, sample_pdf
from tests.test_reader_v2_browser import sample_book, seed
from tests.test_webxdc_browser import browser_fixture


def show_reading_area(page, selector):
    if selector == '.book-reading':
        # Position restoration is completed on an animation frame and then
        # released after its scroll events; don't fight it with test scrolling.
        page.locator('.book-reading').wait_for(state='attached')
        page.locator('.book-reader[aria-busy="false"]').wait_for(state='attached')
    page.evaluate("""selector => {
      const view = document.querySelector('#view');
      const area = document.querySelector(selector);
      view.scrollTop += area.getBoundingClientRect().top - view.getBoundingClientRect().top - 8;
    }""", selector)
    page.wait_for_timeout(200)


def point_on_area(page, selector, side, *, inside=False):
    point = page.evaluate("""({selector,side,inside}) => {
      const view = document.querySelector('#view').getBoundingClientRect();
      const area = document.querySelector(selector).getBoundingClientRect();
      const top = Math.max(view.top, area.top), bottom = Math.min(view.bottom, area.bottom);
      if (bottom - top < 30) throw Error('reading area is not visible');
      const x = inside ? (side === 'left' ? area.left + 10 : side === 'right' ? area.right - 10 : (area.left + area.right) / 2)
                       : (side === 'left' ? view.left + 8 : side === 'right' ? view.right - 8 : (view.left + view.right) / 2);
      return {x, y: (top + bottom) / 2};
    }""", {'selector': selector, 'side': side, 'inside': inside})
    return point['x'], point['y']


def tap_edge(page, selector, side, *, inside=False):
    x, y = point_on_area(page, selector, side, inside=inside)
    page.touchscreen.tap(x, y)


def import_one(page, name, mime, data):
    page.evaluate("go('books',{tab:'saved'})")
    page.locator('.book-file-input').set_input_files({'name': name, 'mimeType': mime, 'buffer': data})
    page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
    book_id = page.evaluate('(async()=> (await BookShelf.all())[0].id)()')
    page.get_by_role('button', name='Abrir', exact=True).click()
    return book_id


@pytest.mark.parametrize('engine,locale,width', [
    ('chromium', 'es', 280), ('webkit', 'es', 320), ('chromium', 'en', 320), ('webkit', 'en', 390),
])
def test_countdown_is_the_only_first_card_and_refreshes_honestly(engine, locale, width):
    with browser_fixture(locale, {'width': width, 'height': 640}, engine=engine, touch=True) as (page, errors):
        page.emulate_media(reduced_motion='reduce')
        card = page.locator('.home-banner-slot .home-countdown')
        value = card.locator('[data-restart-countdown]')
        assert card.count() == value.count() == 1
        assert page.locator('.home-status [data-restart-countdown], .home-status .home-countdown').count() == 0
        assert card.locator('.hero-action, h2, p').count() == 0
        assert card.locator('.home-countdown-label').inner_text() == ('Reinicio en' if locale == 'es' else 'Restart in')
        assert ('aprox.' if locale == 'es' else 'approx.') in value.inner_text()
        first = value.inner_text()
        page.wait_for_function("first => document.querySelector('.home-countdown [data-restart-countdown]').textContent !== first", arg=first, timeout=5000)
        page.evaluate('healthObservation = null')
        page.wait_for_function("() => !document.querySelector('.home-countdown [data-restart-countdown]').textContent.includes(':')", timeout=5000)
        assert value.inner_text() == ('desconocido' if locale == 'es' else 'unknown')
        before = page.evaluate("__sentUpdates.filter(u=>u.payload?.op==='hello').length")
        card.tap()
        page.wait_for_function("before => __sentUpdates.filter(u=>u.payload?.op==='hello').length === before+1", arg=before)
        page.wait_for_function("() => document.querySelector('.home-countdown [data-restart-countdown]').textContent.includes(':')", timeout=5000)
        # Large latency displays a range instead of an invented exact time;
        # even that longer label fits a small screen without clipping.
        page.evaluate("""() => {
          const c=healthClock();
          healthObservation={server:100,restart:3700,start:{mono:c.mono-60000,wall:c.wall-60000},end:c};
        }""")
        page.wait_for_function("() => document.querySelector('.home-countdown [data-restart-countdown]').textContent.includes('–')", timeout=5000)
        assert card.evaluate('el=>el.scrollWidth <= el.clientWidth + 1')
        assert value.evaluate("el=>el.getBoundingClientRect().bottom <= el.closest('.home-countdown').getBoundingClientRect().bottom+1")
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        page.locator('.home-banner-controls button').nth(1).tap()
        assert page.locator('.home-banner-slot [data-restart-countdown]').count() == 0
        assert page.locator('.home-status [data-restart-countdown]').count() == 0
        page.locator('.home-banner-controls button').first.tap()
        assert page.locator('.home-banner-slot [data-restart-countdown]').count() == 1
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['legacy', 'txt'])
def test_text_edges_change_parts_without_top_bar_and_keep_progress(engine, kind):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        if kind == 'legacy':
            book = sample_book()
            seed(page, book)
            page.evaluate("go('books',{tab:'saved'})")
            page.get_by_role('button', name='Abrir', exact=True).click()
            book_id = book['id']
        else:
            data = ('Primera línea del original.\n\n' + ''.join(
                f'Párrafo TXT {i}: palabras, acentos y una historia.\n\n' for i in range(240))).encode()
            book_id = import_one(page, 'lectura.txt', 'text/plain', data)
        page.locator('.book-nav').wait_for(state='attached')
        assert page.locator('.book-nav').count() == 1
        assert page.evaluate("""() => !!(document.querySelector('.book-reading').compareDocumentPosition(
          document.querySelector('.book-nav')) & Node.DOCUMENT_POSITION_FOLLOWING)""")
        assert page.locator('.book-page-label').inner_text().startswith('Parte 1 /')
        assert page.get_by_text('Toca los bordes', exact=False).count() == 1
        show_reading_area(page, '.book-reading')
        before_books = page.evaluate("__sentUpdates.filter(u=>u.payload?.op==='books').length")
        start_scroll = page.evaluate("document.querySelector('#view').scrollTop")
        tap_edge(page, '.book-reading', 'center')
        assert page.evaluate("document.querySelector('#view').scrollTop") == start_scroll
        tap_edge(page, '.book-reading', 'right')
        page.wait_for_function("old => document.querySelector('#view').scrollTop > old + 50", arg=start_scroll)
        page.wait_for_function("id => store.get('book.offset.'+id) > 0", arg=book_id)
        assert page.locator('.book-page-label').inner_text().startswith('Parte 1 /')
        assert page.evaluate("id => store.get('book.offset.'+id)", book_id) < 2000
        tap_edge(page, '.book-reading', 'left')
        page.wait_for_function("old => document.querySelector('#view').scrollTop <= old + 2", arg=start_scroll)
        # Reaching the bottom of a long text part, then tapping right, opens
        # the next part. Left from its start returns to the previous part's end.
        page.evaluate("""() => {const v=document.querySelector('#view'),r=document.querySelector('.book-reading');
          v.scrollTop += r.getBoundingClientRect().bottom-v.getBoundingClientRect().bottom+8;}""")
        page.wait_for_timeout(250)
        tap_edge(page, '.book-reading', 'right')
        page.wait_for_function("id => store.get('book.page.'+id) === 1", arg=book_id)
        page.wait_for_timeout(200)
        assert page.locator('.book-page-label').inner_text().startswith('Parte 2 /')
        assert page.evaluate("id => store.get('book.offset.'+id) > 2000", book_id)
        assert page.evaluate("""() => {const v=document.querySelector('#view').getBoundingClientRect();
          const r=document.querySelector('.book-reading').getBoundingClientRect();return Math.abs(r.top-v.top) < 30;}""")
        tap_edge(page, '.book-reading', 'left')
        page.wait_for_function("id => store.get('book.page.'+id) === 0", arg=book_id)
        page.wait_for_timeout(200)
        assert page.evaluate("id => store.get('book.offset.'+id) > 2000", book_id)
        tap_edge(page, '.book-reading', 'right')
        page.wait_for_function("id => store.get('book.page.'+id) === 1", arg=book_id)
        page.wait_for_timeout(200)
        tap_edge(page, '.book-reading', 'right')
        saved = page.evaluate("id => store.get('book.offset.'+id)", book_id)
        assert saved > 4500
        assert page.evaluate("__sentUpdates.filter(u=>u.payload?.op==='books').length") == before_books
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-page-label').wait_for(state='attached')
        assert page.locator('.book-page-label').inner_text().startswith('Parte 2 /')
        page.reload()
        page.wait_for_function('window.__michiflinStartupDone && cache.at > 0 && !syncing')
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-page-label').wait_for(state='attached')
        assert page.locator('.book-page-label').inner_text().startswith('Parte 2 /')
        assert abs(page.evaluate("id => store.get('book.offset.'+id)", book_id) - saved) < 500
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_text_edges_do_not_steal_selection_scroll_long_press_or_controls(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book = sample_book()
        seed(page, book)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        show_reading_area(page, '.book-reading')
        page.evaluate("""() => {const node=document.querySelector('.book-reading p').firstChild;
          const r=document.createRange();r.setStart(node,0);r.setEnd(node,8);
          const s=getSelection();s.removeAllRanges();s.addRange(r);} """)
        start_scroll = page.evaluate("document.querySelector('#view').scrollTop")
        assert page.evaluate('getSelection().toString().length') > 0
        tap_edge(page, '.book-reading', 'right')
        assert page.evaluate("document.querySelector('#view').scrollTop") == start_scroll
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        page.evaluate('getSelection().removeAllRanges()')
        x, y = point_on_area(page, '.book-reading', 'right')
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(700)
        page.mouse.up()
        assert page.evaluate("document.querySelector('#view').scrollTop") == start_scroll
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y - 80, steps=8)
        page.mouse.up()
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        page.evaluate('getSelection().removeAllRanges()')
        old_scroll = page.evaluate("document.querySelector('#view').scrollTop")
        page.mouse.move(x, y)
        page.mouse.wheel(0, 180)
        page.wait_for_timeout(250)
        assert page.evaluate("document.querySelector('#view').scrollTop") > old_scroll
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        page.locator('.book-toolbox > summary').click()
        assert page.locator('.book-toolbox').evaluate('el=>el.open')
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        # The focusable text area and the bottom buttons are keyboard alternatives.
        page.locator('.book-reading').focus()
        before_key = page.evaluate("document.querySelector('#view').scrollTop")
        page.keyboard.press('ArrowRight')
        page.wait_for_function("old => document.querySelector('#view').scrollTop > old + 50", arg=before_key)
        after_key = page.evaluate("document.querySelector('#view').scrollTop")
        page.keyboard.press('ArrowLeft')
        page.wait_for_function("old => document.querySelector('#view').scrollTop < old - 50", arg=after_key)
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        page.get_by_role('button', name='Siguiente', exact=True).click()
        page.wait_for_function("store.get('book.page.fixture:read') === 1")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_pdf_edges_page_from_stage_and_screen_margin_and_preserve_state(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id = import_one(page, 'paginas.pdf', 'application/pdf', sample_pdf())
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert page.locator('.book-file-nav').count() == 1
        assert page.evaluate("""() => !!(document.querySelector('.book-file-stage').compareDocumentPosition(
          document.querySelector('.book-file-nav')) & Node.DOCUMENT_POSITION_FOLLOWING)""")
        show_reading_area(page, '.book-file-stage')
        tap_edge(page, '.book-file-stage', 'left', inside=True)  # already at first page
        assert 'Página 1 / 3' in page.locator('.book-file-page').inner_text()
        tap_edge(page, '.book-file-stage', 'right', inside=True)
        page.wait_for_function("id => store.get('book.filePosition.'+id) === 2", arg=book_id)
        assert 'Página 2 / 3' in page.locator('.book-file-page').inner_text()
        tap_edge(page, '.book-file-stage', 'right')  # outer screen edge, not just the canvas
        page.wait_for_function("id => store.get('book.filePosition.'+id) === 3", arg=book_id)
        tap_edge(page, '.book-file-stage', 'right', inside=True)
        assert page.evaluate("id => store.get('book.filePosition.'+id)", book_id) == 3
        tap_edge(page, '.book-file-stage', 'left', inside=True)
        page.wait_for_function("id => store.get('book.filePosition.'+id) === 2", arg=book_id)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert 'Página 2 / 3' in page.locator('.book-file-page').inner_text()
        show_reading_area(page, '.book-file-stage')
        page.locator('.book-pdf-scroll').focus()
        page.keyboard.press('ArrowRight')
        page.wait_for_function("id => store.get('book.filePosition.'+id) === 3", arg=book_id)
        page.keyboard.press('ArrowLeft')
        page.wait_for_function("id => store.get('book.filePosition.'+id) === 2", arg=book_id)
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_epub_margins_turn_pages_without_covering_links_or_selection(engine):
    chapter = ('<h1>Primer capítulo</h1><p><a href="#same" style="display:block;width:100%">Enlace local</a></p>' +
               ''.join(f'<p>Párrafo largo {i}: una historia de prueba para pasar páginas sin red.</p>' for i in range(35)) +
               '<h2 id="same">Destino del enlace</h2>' +
               ''.join(f'<p>Párrafo final {i}: otra historia para pasar páginas sin red.</p>' for i in range(65)))
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id = import_one(page, 'cuento.epub', 'application/epub+zip', sample_epub(chapter))
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert page.locator('.book-file-nav').count() == 1
        assert page.evaluate("""() => !!(document.querySelector('.book-file-stage').compareDocumentPosition(
          document.querySelector('.book-file-nav')) & Node.DOCUMENT_POSITION_FOLLOWING)""")
        show_reading_area(page, '.book-file-stage')
        frame = page.frame_locator('.book-file-stage iframe').first
        label = page.locator('.book-file-page').inner_text()
        assert re.search(r'página 1 / ([2-9]|[1-9][0-9]+)', label), label
        first_position = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        assert first_position
        # The clickable text is not covered by an invisible gesture overlay.
        link = frame.locator('a').first
        box = link.bounding_box()
        x, y = box['x'] + 3, box['y'] + box['height'] / 2
        assert page.evaluate("({x,y}) => document.elementFromPoint(x,y).tagName", {'x': x, 'y': y}) == 'IFRAME'
        assert link.evaluate("el => document.elementFromPoint(el.getBoundingClientRect().left+3, el.getBoundingClientRect().top+3) === el")
        frame.locator('h1').evaluate("""node=>{const r=document.createRange();r.selectNodeContents(node);
          const s=window.getSelection();s.removeAllRanges();s.addRange(r)}""")
        tap_edge(page, '.book-file-stage', 'right')
        assert 'página 1 /' in page.locator('.book-file-page').inner_text()
        frame.locator('h1').evaluate('()=>window.getSelection().removeAllRanges()')
        # Tap the narrow gutter immediately outside the iframe, not its text.
        stage = page.locator('.book-file-stage').bounding_box()
        page.touchscreen.tap(stage['x'] + stage['width'] + 7, stage['y'] + stage['height'] / 2)
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('página 2 /')", timeout=15000)
        second_position = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        assert second_position and second_position != first_position
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('página 3 /')", timeout=15000)
        stage = page.locator('.book-file-stage').bounding_box()
        page.touchscreen.tap(stage['x'] - 7, stage['y'] + stage['height'] / 2)
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('página 2 /')", timeout=15000)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert 'página 2 /' in page.locator('.book-file-page').inner_text()
        assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
        page.locator('.book-file-area').focus()
        page.keyboard.press('ArrowRight')
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('página 3 /')", timeout=15000)
        page.keyboard.press('ArrowLeft')
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('página 2 /')", timeout=15000)
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_touch_fallback_without_pointer_events_still_turns_once(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True,
                         init_script="Object.defineProperty(window,'PointerEvent',{value:undefined,configurable:true});") as (page, errors):
        seed(page, sample_book())
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        show_reading_area(page, '.book-reading')
        before = page.evaluate("document.querySelector('#view').scrollTop")
        tap_edge(page, '.book-reading', 'right')
        page.wait_for_function("old => document.querySelector('#view').scrollTop > old+50", arg=before)
        page.wait_for_timeout(400)  # no duplicate compatibility mouse turn
        moved = page.evaluate("old => document.querySelector('#view').scrollTop-old", before)
        height = page.evaluate("document.querySelector('#view').clientHeight")
        assert moved <= height * .82 + 2
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        assert errors == []
