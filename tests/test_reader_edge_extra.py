"""Additional page boundaries, touch fallback and zoom regressions."""
from __future__ import annotations

import pytest

from tests.test_book_originals import sample_epub, sample_pdf
from tests.test_reader_v2_browser import sample_book, seed
from tests.book_reader_harness import open_test_reader
from tests.test_webxdc_browser import browser_fixture
from tests.test_welcome_reader_edges import import_one, show_reading_area, tap_edge


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('fallback', [False, True])
def test_epub_margins_cross_chapter_boundaries_even_without_pointer_events(engine, fallback):
    init = "Object.defineProperty(window,'PointerEvent',{value:undefined,configurable:true});" if fallback else ''
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True, init_script=init) as (page, errors):
        book_id = import_one(page, 'dos-capitulos.epub', 'application/epub+zip', sample_epub())
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        show_reading_area(page, '.book-file-stage')
        assert 'Sección 1 / 2' in page.locator('.book-file-page').inner_text()
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('Sección 2 / 2')", timeout=15000)
        page.wait_for_timeout(350)
        at_end = page.evaluate("id=>store.get('book.filePosition.'+id)", book_id)
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_timeout(350)
        assert page.evaluate("id=>store.get('book.filePosition.'+id)", book_id) == at_end
        tap_edge(page, '.book-file-stage', 'left')
        page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('Sección 1 / 2')", timeout=15000)
        assert page.frame_locator('.book-file-stage iframe').first.locator('h1').inner_text() == 'Capítulo uno'
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_pdf_zoom_keeps_keyboard_pan_and_resets_vertical_scroll_on_page_turn(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id = import_one(page, 'zoom.pdf', 'application/pdf', sample_pdf())
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        page.locator('.book-toolbox > summary').click()
        page.locator('summary').get_by_text('Página y zoom', exact=True).click()
        page.get_by_label('Zoom del PDF').fill('200')
        page.wait_for_function("() => {const w=document.querySelector('.book-pdf-scroll');return w.scrollWidth>w.clientWidth+30 && w.scrollHeight>w.clientHeight+30}", timeout=15000)
        show_reading_area(page, '.book-file-stage')
        page.evaluate("() => {const w=document.querySelector('.book-pdf-scroll');w.scrollLeft=100;w.scrollTop=100}")
        wrap = page.locator('.book-pdf-scroll')
        wrap.focus()
        page.keyboard.press('ArrowLeft')
        page.wait_for_function("() => document.querySelector('.book-pdf-scroll').scrollLeft < 100", timeout=5000)
        assert page.evaluate("id=>store.get('book.filePosition.'+id)", book_id) == 1
        assert page.evaluate("document.querySelector('.book-pdf-scroll').scrollTop") > 0
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_function("id=>store.get('book.filePosition.'+id) === 2", arg=book_id)
        assert page.evaluate("document.querySelector('.book-pdf-scroll').scrollTop") == 0
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['pdf', 'epub'])
def test_bottom_controls_reflow_without_overlap_at_280px_and_large_text(engine, kind):
    raw = sample_pdf() if kind == 'pdf' else sample_epub()
    mime = 'application/pdf' if kind == 'pdf' else 'application/epub+zip'
    with browser_fixture('es', {'width': 280, 'height': 640}, engine=engine, touch=True) as (page, errors):
        import_one(page, 'estrecho.' + kind, mime, raw)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        page.add_style_tag(content='.book-file-nav .btn, .book-file-page {font-size: 18px !important}')
        assert page.locator('.book-file-nav').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        assert page.locator('#view').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        assert page.evaluate("""() => {const n=document.querySelector('.book-file-nav');
          const label=n.querySelector('.book-file-page').getBoundingClientRect();
          return [...n.querySelectorAll('button')].every(b=>b.getBoundingClientRect().top>=label.bottom-1)}""")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_epub_waits_for_restored_location_and_keeps_early_touch(engine):
    chapter = '<h1>Prueba de restauración</h1>' + ''.join(
        f'<p>Párrafo {i}: una historia sintética suficientemente larga para varias páginas.</p>' for i in range(100))
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id = import_one(page, 'restauracion.epub', 'application/epub+zip', sample_epub(chapter))
        page.wait_for_function("document.querySelector('.book-file-page')?.textContent.includes('página 1 /')", timeout=30000)
        show_reading_area(page, '.book-file-stage')
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('página 2 /')", timeout=15000)
        saved = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        page.evaluate("go('books',{tab:'saved'})")
        # Hold only the host's relocated callbacks; the EPUB engine still
        # renders normally, reproducing a slow restoration deterministically.
        page.evaluate("""() => {
          const factory=window.ePub; window.__holdEpubLocations=true; window.__heldEpubLocations=[];
          window.ePub=function(...args) {
            const book=factory.apply(this,args), render=book.renderTo.bind(book);
            book.renderTo=(...values) => {
              const rendition=render(...values), on=rendition.on.bind(rendition);
              rendition.on=(event,handler) => event !== 'relocated' ? on(event,handler) : on(event,location => {
                if (window.__holdEpubLocations) window.__heldEpubLocations.push(() => handler(location));
                else handler(location);
              });
              return rendition;
            };
            return book;
          };
        }""")
        open_test_reader(page)
        page.wait_for_function('window.__heldEpubLocations.length > 0', timeout=30000)
        assert 'Abriendo' in page.locator('.book-file-status').inner_text()
        assert page.locator('.book-file-reader').get_attribute('aria-busy') == 'true'
        page.evaluate("""() => {const v=document.querySelector('#view'),s=document.querySelector('.book-file-stage');
          v.scrollTop += s.getBoundingClientRect().top-v.getBoundingClientRect().top-8;}""")
        tap_edge(page, '.book-file-stage', 'right')
        assert page.evaluate("id => store.get('book.filePosition.'+id)", book_id) == saved
        page.evaluate("""() => {window.__holdEpubLocations=false;
          for (const release of window.__heldEpubLocations.splice(0)) release();}""")
        page.locator('.book-file-reader[aria-busy="false"]').wait_for(state='attached', timeout=15000)
        page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('página 3 /')", timeout=15000)
        page.wait_for_function("({id,old}) => store.get('book.filePosition.'+id) !== old", arg={'id': book_id, 'old': saved})
        confirmed = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        # A tap queued during the next opening must not overwrite the confirmed
        # position if the person closes before the relocation arrives.
        page.evaluate("go('books',{tab:'saved'})")
        page.evaluate("() => {window.__holdEpubLocations=true;window.__heldEpubLocations=[];}")
        open_test_reader(page)
        page.wait_for_function('window.__heldEpubLocations.length > 0', timeout=30000)
        page.evaluate("""() => {const v=document.querySelector('#view'),s=document.querySelector('.book-file-stage');
          v.scrollTop += s.getBoundingClientRect().top-v.getBoundingClientRect().top-8;}""")
        tap_edge(page, '.book-file-stage', 'right')
        page.evaluate("go('books',{tab:'saved'})")
        page.evaluate("""() => {window.__holdEpubLocations=false;
          for (const release of window.__heldEpubLocations.splice(0)) release();}""")
        page.wait_for_timeout(250)
        assert page.evaluate("id => store.get('book.filePosition.'+id)", book_id) == confirmed
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_epub_saved_visible_position_survives_a_viewport_change(engine):
    chapter = '<h1>Inicio</h1>' + ''.join(
        f'<p>Párrafo {i}: varias palabras de una historia sintética para leer.</p>' for i in range(90))
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book_id = import_one(page, 'ancho.epub', 'application/epub+zip', sample_epub(chapter))
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        page.evaluate("go('books',{tab:'saved'})")
        page.evaluate("""() => {const factory=window.ePub;window.ePub=function(...args){
          const book=factory.apply(this,args),render=book.renderTo.bind(book);
          book.renderTo=(...values) => window.__visibleRendition=render(...values);return book;};}""")
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        show_reading_area(page, '.book-file-stage')
        tap_edge(page, '.book-file-stage', 'right')
        page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('página 2 /')", timeout=15000)
        saved = page.evaluate("id => store.get('book.filePosition.'+id)", book_id)
        visible = """cfi => {
          const stage=document.querySelector('.book-file-stage').getBoundingClientRect();
          const content=window.__visibleRendition.getContents()[0], frame=content.document.defaultView.frameElement.getBoundingClientRect();
          const rect=content.range(cfi).getBoundingClientRect();
          return rect.height>0 && frame.left+rect.right>=stage.left-1 && frame.left+rect.left<=stage.right+1 &&
            frame.top+rect.bottom>=stage.top-1 && frame.top+rect.top<=stage.bottom+1;
        }"""
        assert page.evaluate(visible, saved)
        page.evaluate("go('books',{tab:'saved'})")
        page.set_viewport_size({'width': 390, 'height': 844})
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        page.wait_for_function(visible, arg=saved, timeout=15000)
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_text_ready_state_tracks_delayed_restoration_before_positioning(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        seed(page, sample_book())
        page.evaluate("""() => {
          const raf=window.requestAnimationFrame.bind(window);
          window.requestAnimationFrame=callback => raf(time => setTimeout(() => callback(time),400));
        }""")
        page.evaluate("go('books',{tab:'saved'})")
        open_test_reader(page)
        page.locator('.book-reader[aria-busy="true"]').wait_for(state='attached')
        show_reading_area(page, '.book-reading')
        assert page.locator('.book-reader').get_attribute('aria-busy') == 'false'
        assert page.evaluate("""() => {const v=document.querySelector('#view').getBoundingClientRect();
          const r=document.querySelector('.book-reading').getBoundingClientRect();return Math.abs(r.top-v.top)<20;}""")
        tap_edge(page, '.book-reading', 'right')
        page.wait_for_function("store.get('book.offset.fixture:read') > 100")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['legacy', 'txt'])
def test_real_touch_before_restore_is_applied_after_saved_position(engine, kind):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        if kind == 'legacy':
            book = sample_book()
            seed(page, book, offset=600)
            book_id = book['id']
            page.evaluate("go('books',{tab:'saved'})")
        else:
            raw = ('Primera línea.\n\n' + ''.join(
                f'Párrafo TXT {i}: una historia para leer tranquilamente.\n\n' for i in range(220))).encode()
            page.evaluate("go('books',{tab:'saved'})")
            page.locator('.book-file-input').set_input_files({'name': 'lectura.txt', 'mimeType': 'text/plain', 'buffer': raw})
            page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
            book_id = page.evaluate('(async()=> (await BookShelf.all())[0].id)()')
            page.evaluate("id => store.set('book.offset.'+id, 600)", book_id)
        # Keep restoration pending while the browser dispatches an actual touch.
        page.evaluate("""() => {const raf=window.requestAnimationFrame.bind(window);
          window.requestAnimationFrame=callback => raf(time => setTimeout(() => callback(time),1500));}""")
        open_test_reader(page)
        page.locator('.book-reader[aria-busy="true"]').wait_for(state='attached')
        point = page.evaluate("""() => {
          const view=document.querySelector('#view'), reading=document.querySelector('.book-reading');
          view.scrollTop += reading.getBoundingClientRect().top-view.getBoundingClientRect().top-8;
          const v=view.getBoundingClientRect(), r=reading.getBoundingClientRect();
          return {x:v.right-8,y:(Math.max(v.top,r.top)+Math.min(v.bottom,r.bottom))/2};
        }""")
        assert page.locator('.book-reader').get_attribute('aria-busy') == 'true'
        page.touchscreen.tap(point['x'], point['y'])
        assert page.locator('.book-reader').get_attribute('aria-busy') == 'true'
        page.locator('.book-reader[aria-busy="false"]').wait_for(state='attached', timeout=5000)
        page.wait_for_function("id => store.get('book.offset.'+id) > 680", arg=book_id, timeout=3000)
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_closing_during_pending_restore_keeps_last_confirmed_position(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        book = sample_book()
        seed(page, book, offset=600)
        page.evaluate("go('books',{tab:'saved'})")
        page.evaluate("""() => {window.__oldRAF=window.requestAnimationFrame.bind(window);
          window.requestAnimationFrame=callback => __oldRAF(time => setTimeout(() => callback(time),1200));}""")
        open_test_reader(page)
        page.locator('.book-reader[aria-busy="true"]').wait_for(state='attached')
        point = page.evaluate("""() => {const view=document.querySelector('#view'), reading=document.querySelector('.book-reading');
          view.scrollTop += reading.getBoundingClientRect().top-view.getBoundingClientRect().top-8;
          const v=view.getBoundingClientRect(),r=reading.getBoundingClientRect();
          return {x:v.right-8,y:(Math.max(v.top,r.top)+Math.min(v.bottom,r.bottom))/2};}""")
        page.touchscreen.tap(point['x'], point['y'])
        assert page.locator('.book-reader').get_attribute('aria-busy') == 'true'
        page.evaluate("go('books',{tab:'saved'})")
        page.wait_for_timeout(1500)
        assert page.evaluate("store.get('book.offset.fixture:read')") == 600
        page.evaluate('() => { window.requestAnimationFrame = window.__oldRAF; }')
        open_test_reader(page)
        page.locator('.book-reader[aria-busy="false"]').wait_for(state='attached')
        assert abs(page.evaluate("store.get('book.offset.fixture:read')") - 600) < 80
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_text_tap_saves_position_even_if_reader_closes_in_the_same_event_turn(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        seed(page, sample_book())
        page.evaluate("go('books',{tab:'saved'})")
        open_test_reader(page)
        show_reading_area(page, '.book-reading')
        result = page.evaluate("""() => {
          const v=document.querySelector('#view').getBoundingClientRect();
          const r=document.querySelector('.book-reading').getBoundingClientRect();
          const x=v.right-8,y=(Math.max(v.top,r.top)+Math.min(v.bottom,r.bottom))/2;
          const target=document.elementFromPoint(x,y);
          const props={bubbles:true,pointerId:1,pointerType:'touch',isPrimary:true,button:0,clientX:x,clientY:y};
          target.dispatchEvent(new PointerEvent('pointerdown',props));
          target.dispatchEvent(new PointerEvent('pointerup',props));
          const before=store.get('book.offset.fixture:read');
          go('books',{tab:'saved'});
          return {before,after:store.get('book.offset.fixture:read')};
        }""")
        assert result['before'] > 100
        assert result['after'] == result['before']
        open_test_reader(page)
        page.wait_for_function("store.get('book.offset.fixture:read') > 100")
        assert abs(page.evaluate("store.get('book.offset.fixture:read')") - result['before']) < 200
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_multitouch_cancel_and_reader_teardown_do_not_turn_pages(engine):
    with browser_fixture('es', {'width': 320, 'height': 640}, engine=engine, touch=True) as (page, errors):
        seed(page, sample_book())
        page.evaluate("go('books',{tab:'saved'})")
        open_test_reader(page)
        show_reading_area(page, '.book-reading')
        before_scroll = page.evaluate("document.querySelector('#view').scrollTop")
        page.evaluate("""() => {
          const v=document.querySelector('#view').getBoundingClientRect();
          const r=document.querySelector('.book-reading').getBoundingClientRect();
          const x=v.right-8,y=(Math.max(v.top,r.top)+Math.min(v.bottom,r.bottom))/2;
          const target=document.elementFromPoint(x,y);
          const emit=(name,id,primary)=>target.dispatchEvent(new PointerEvent(name,{bubbles:true,pointerId:id,
            pointerType:'touch',isPrimary:primary,button:0,clientX:x,clientY:y}));
          emit('pointerdown',1,true);emit('pointerdown',2,false);emit('pointerup',1,true);emit('pointerup',2,false);
        }""")
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        assert page.evaluate("document.querySelector('#view').scrollTop") == before_scroll
        page.evaluate("goTab('home')")
        page.evaluate("""() => {const view=document.querySelector('#view'),r=view.getBoundingClientRect();
          const props={bubbles:true,pointerId:1,pointerType:'touch',isPrimary:true,button:0,
            clientX:r.right-8,clientY:(r.top+r.bottom)/2};
          view.dispatchEvent(new PointerEvent('pointerdown',props));view.dispatchEvent(new PointerEvent('pointerup',props));}""")
        assert page.evaluate("store.get('book.page.fixture:read')") == 0
        assert page.evaluate('current.view') == 'home'
        assert errors == []
