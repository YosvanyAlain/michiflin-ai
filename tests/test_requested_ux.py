"""Requested UI behavior, using synthetic data and an emulated WebXDC host.

These tests do not prove a native Delta Chat/ArcaneChat export or email delivery.
"""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture, install_live_frame_fixture


@pytest.mark.parametrize('engine,width', [('chromium', 280), ('webkit', 320), ('chromium', 390)])
def test_countdown_navigation_freshness_color_and_combined_status(engine, width):
    with browser_fixture('es', {'width': width, 'height': 740}, engine=engine, touch=True) as (page, errors):
        page.emulate_media(reduced_motion='reduce')
        card = page.locator('.home-banner-slot .home-countdown')
        assert card.locator('[data-restart-countdown]').count() == 1
        assert page.locator('.home-status').count() == 0
        assert 'en cola' in card.locator('.home-countdown-queue').inner_text()
        assert 'Actualizado hace' in card.locator('.home-countdown-updated').inner_text()
        page.evaluate('''() => {
          const c=healthClock(); healthObservation={server:100,restart:3700,
            start:{mono:c.mono-20000,wall:c.wall-20000},end:c}; paintCountdown(document.querySelector('.home-countdown'));
        }''')
        page.wait_for_function("document.querySelector('[data-countdown-freshness]').textContent.includes('lenta')")
        label = card.locator('[data-restart-countdown]').inner_text()
        assert 'aprox.' in label and label.count(':') == 2 and '–' not in label
        assert card.evaluate('n=>n.scrollWidth<=n.clientWidth+1')
        # A focused card is not rebuilt by the carousel, but status still moves.
        page.locator('.home-countdown-action').focus()
        page.evaluate("setJobs([{id:77,status:'running',title:'Fixture'}], false);cache.at=now()-90;bus.emit('hello')")
        page.wait_for_function("document.querySelector('.home-countdown-queue').textContent.includes('1 en cola')")
        assert page.locator('.home-countdown-updated [data-since]').get_attribute('data-since') == str(page.evaluate('cache.at'))
        before = page.evaluate('restartEstimate().seconds')
        page.evaluate("goTab('ai');goTab('home')")
        assert page.evaluate('restartEstimate().seconds') <= before
        assert 'aprox.' in page.locator('.home-countdown [data-restart-countdown]').inner_text()
        page.evaluate('''() => { const c=healthClock(); healthObservation={server:100,restart:3700,
          start:{mono:c.mono-122000,wall:c.wall-122000}, end:{mono:c.mono-121000,wall:c.wall-121000}};
          paintCountdown(document.querySelector('.home-countdown')); }''')
        assert 'Datos antiguos' in page.locator('[data-countdown-freshness]').inner_text()
        page.evaluate('''() => { const c=healthClock();healthObservation={server:100,restart:200,
          start:{mono:c.mono-1000,wall:c.wall-1000},end:c};paintCountdown(document.querySelector('.home-countdown')); }''')
        assert page.locator('.home-countdown').get_attribute('data-urgency') == 'critical'
        page.locator('.home-countdown-queue').click()
        assert page.evaluate('current.view') == 'downloads'
        assert errors == []


def install_book_fixture(page):
    page.evaluate('''() => {
      const original=bridge.call.bind(bridge);bridge.call=async(op,args)=>{
        if(op!=='books')return original(op,args);
        if(args.do==='search')return {books:[{id:'fixture:1',source:'Fixture',title:'Un libro',authors:['Autor'],
          url:'https://example.invalid/book',downloads:[{format:'txt',url:'https://example.invalid/book.txt'}]}],more:false};
        if(args.do==='original_start') {const blob=new Blob(['Contenido íntegro de prueba'],{type:'text/plain'});
          return {transfer:'fixture',format:'txt',size:blob.size,index:0,count:1,sha256:await FileKitCrypto.hashBlob(blob),data:await blobToBase64(blob)};}
        return {};
      };go('books',{q:'libro'});
    }''')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_badge_retained_until_open_and_no_internal_reader(engine):
    with browser_fixture('es', {'width': 320, 'height': 740}, engine=engine) as (page, errors):
        install_book_fixture(page)
        page.get_by_role('heading', name='Un libro', exact=True).wait_for()
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Guardado', exact=True).wait_for()
        badge = page.locator('[data-key=saved] .book-new-count')
        assert badge.inner_text() == '1'
        assert '1 libro nuevo' in page.locator('[data-key=saved]').get_attribute('aria-label')
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        page.evaluate("goTab('home');go('books')")
        assert page.locator('[data-key=saved] .book-new-count').inner_text() == '1'
        page.locator('[data-key=saved]').click()
        assert page.locator('[data-key=saved] .book-new-count').count() == 0
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        assert page.locator('.book-reader, .book-reading').count() == 0
        assert page.get_by_role('button', name='Exportar original').count() == 1
        assert page.evaluate('(async()=> (await BookShelf.all())[0].original.size)()') > 0
        page.reload()
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate("go('books',{tab:'saved'})")
        assert page.get_by_role('heading', name='Un libro', exact=True).count() == 1
        assert page.locator('[data-key=saved] .book-new-count').count() == 0
        assert errors == []


def test_download_completed_after_navigation_updates_new_library_view():
    with browser_fixture('es', {'width': 390, 'height': 740}) as (page, errors):
        page.evaluate('''() => {
          const original=bridge.call.bind(bridge);bridge.call=async(op,args)=>{
            if(op!=='books')return original(op,args);
            if(args.do==='search')return {books:[{id:'fixture:later',source:'Fixture',title:'Llegó después',authors:[],
              url:'https://example.invalid/book',downloads:[{format:'txt',url:'https://example.invalid/book.txt'}]}],more:false};
            if(args.do==='original_start')return new Promise(resolve=>{window.__finishDownload=async()=>{
              const blob=new Blob(['prueba'],{type:'text/plain'});resolve({transfer:'fixture',format:'txt',size:blob.size,
                index:0,count:1,sha256:await FileKitCrypto.hashBlob(blob),data:await blobToBase64(blob)});};});
            return {};
          };go('books',{q:'libro'});
        }''')
        page.get_by_role('heading', name='Llegó después').wait_for()
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.wait_for_function('window.__finishDownload')
        page.evaluate("goTab('home');go('books')")
        page.evaluate('window.__finishDownload()')
        page.locator('[data-key=saved] .book-new-count').get_by_text('1').wait_for()
        page.locator('[data-key=saved]').click()
        page.get_by_role('heading', name='Llegó después').wait_for()
        assert page.locator('.book-new-count').count() == 0 and errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_late_download_not_hidden_by_older_initial_shelf_snapshot(engine):
    with browser_fixture('es', {'width': 390, 'height': 740}, engine=engine) as (page, errors):
        page.evaluate('''() => {
          const original=bridge.call.bind(bridge);bridge.call=async(op,args)=>{
            if(op!=='books')return original(op,args);
            if(args.do==='search')return {books:[{id:'fixture:race',source:'Fixture',title:'Llegada tardía',authors:[],
              url:'https://example.invalid/book',downloads:[{format:'txt',url:'https://example.invalid/book.txt'}]}],more:false};
            if(args.do==='original_start')return new Promise(resolve=>{window.__finishDownload=async()=>{
              const blob=new Blob(['prueba'],{type:'text/plain'});resolve({transfer:'fixture',format:'txt',size:blob.size,
                index:0,count:1,sha256:await FileKitCrypto.hashBlob(blob),data:await blobToBase64(blob)});};});
            return {};
          };go('books',{q:'libro'});
        }''')
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.wait_for_function('window.__finishDownload')
        # The initial read returns an empty snapshot, but its oncomplete is held
        # until after a new book commits in the old view.
        page.evaluate('''() => {
          const original = IDBDatabase.prototype.transaction; let held = false;
          IDBDatabase.prototype.transaction = function(store, mode, ...rest) {
            const tx = original.call(this, store, mode, ...rest);
            if (held || store !== 'books' || mode !== 'readonly') return tx;
            held = true;
            return new Proxy(tx, {
              get(target, property) {
                const value = Reflect.get(target, property, target);
                return typeof value === 'function' ? value.bind(target) : value;
              },
              set(target, property, value) {
                if (property === 'oncomplete') {
                  target.oncomplete = event => { window.__releaseOldShelf = () => value.call(target, event); };
                  return true;
                }
                return Reflect.set(target, property, value, target);
              }
            });
          };
          goTab('home');go('books',{tab:'saved'});
        }''')
        page.wait_for_function('typeof window.__releaseOldShelf === "function"')
        page.evaluate('window.__finishDownload()')
        page.wait_for_function('async () => (await BookShelf.all()).length === 1')
        page.wait_for_timeout(200)
        page.evaluate('window.__releaseOldShelf()')
        page.get_by_role('heading', name='Llegada tardía').wait_for(timeout=30000)
        assert page.locator('.book-new-count').count() == 0 and errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_device_export_uses_host_chooser_without_claiming_saved(engine):
    with browser_fixture('es', {'width': 390, 'height': 740}, engine=engine) as (page, errors):
        page.evaluate('''() => {
          Object.defineProperty(navigator,'canShare',{configurable:true,value:()=>false});
          window.__exportDrafts=[];
          window.webxdc.sendToChat=async draft=>{window.__exportDrafts.push(draft)};
          exportSheet(new Blob(['original'],{type:'text/plain'}),'ejemplo.txt');
        }''')
        page.get_by_role('button', name='Guardar fuera o compartir', exact=False).click()
        page.wait_for_function('window.__exportDrafts.length === 1')
        assert page.evaluate('window.__exportDrafts[0].file.name') == 'ejemplo.txt'
        assert page.evaluate('window.__exportDrafts[0].file.blob.size') == 8
        assert 'guardado' not in page.locator('#toasts').inner_text().lower()
        # A cancelled chooser must not open a second base64 draft.
        page.evaluate('''() => {window.__exportDrafts=[];window.webxdc.sendToChat=async d=>{
          window.__exportDrafts.push(d);throw new DOMException('cancelled','AbortError');};
          exportSheet(new Blob(['x']),'x.txt');}''')
        page.get_by_role('button', name='Guardar fuera o compartir', exact=False).click()
        page.wait_for_function('window.__exportDrafts.length === 1')
        page.wait_for_timeout(100)
        assert page.evaluate('window.__exportDrafts.length') == 1
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_live_is_compact_with_accessible_privacy_and_no_device_link(engine):
    with browser_fixture('es', {'width': 320, 'height': 740}, engine=engine) as (page, errors):
        install_live_frame_fixture(page)
        assert page.locator('.live-brief').is_visible()
        assert not page.locator('.live-privacy').evaluate('el=>el.open')
        assert not page.locator('.live-session-options').evaluate('el=>el.open')
        assert page.locator('.live-device-link').count() == 0
        assert page.get_by_role('link', name='Abrir dirección inicial en el navegador del dispositivo').count() == 0
        page.locator('.live-privacy > summary').click()
        assert page.get_by_text('un lector de pantalla no puede leer', exact=False).is_visible()
        page.locator('.live-session-options > summary').click()
        assert page.get_by_role('button', name='Olvidar sesión', exact=False).count() == 1
        page.get_by_role('button', name='Iniciar navegador').click()
        page.wait_for_function('window.liveWeb.ready')
        assert page.locator('.live-extra-controls').is_visible()
        assert not page.locator('.live-extra-controls').evaluate('el=>el.open')
        page.locator('.live-extra-controls > summary').click()
        assert page.get_by_role('button', name='Guardar sesión', exact=False).count() == 1
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_early_invalid_import_is_not_overwritten_by_initial_shelf_load(engine):
    # Hold the first IndexedDB open callback, select an invalid file, then let
    # the initial shelf load finish. A late redraw must not hide the error.
    with browser_fixture('es', {'width': 390, 'height': 740}, engine=engine) as (page, errors):
        page.evaluate('''() => {
          const original = indexedDB.open.bind(indexedDB);
          let intercept = true;
          indexedDB.open = (...args) => {
            const request = original(...args);
            if (!intercept || args[0] !== 'michiflin-reading-library-v1') return request;
            intercept = false;
            return new Proxy(request, {
              get(target, property) { return Reflect.get(target, property, target); },
              set(target, property, value) {
                if (property === 'onsuccess') {
                  target.onsuccess = event => { window.__releaseShelf = () => value.call(target, event); };
                  return true;
                }
                return Reflect.set(target, property, value, target);
              }
            });
          };
          go('books', {tab:'saved'});
        }''')
        page.wait_for_function('typeof window.__releaseShelf === "function"')
        page.locator('.book-file-input').set_input_files({'name': 'invalid.txt', 'mimeType': 'text/plain', 'buffer': b'\x00'})
        page.wait_for_function("document.querySelector('.book-saved-actions button').disabled")
        page.evaluate('window.__releaseShelf()')
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for(timeout=30000)
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0
        assert errors == []
