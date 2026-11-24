"""Offline reader and migration tests; all books, bridge and accounts are synthetic."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from michiflin import book_library
from tests.test_existing_app_browser import BRIDGE, _handler
from tests.test_reader_epub_view import fixture_epub
from tests.test_webxdc_browser import browser_fixture


def seed(page, book, offset=None, old_page=None, marks=None):
    page.evaluate('''async ({book, offset, oldPage, marks}) => {
      const db = await new Promise((resolve, reject) => {
        const req = indexedDB.open('michiflin-reading-library-v1', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('books', {keyPath:'id'});
        req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error);
      });
      await new Promise((resolve, reject) => {
        const tx=db.transaction('books','readwrite'); tx.objectStore('books').put(book);
        tx.oncomplete=resolve; tx.onabort=()=>reject(tx.error);
      });
      db.close();
      if(offset !== null) store.set('book.offset.'+book.id, offset);
      if(oldPage !== null) store.set('book.page.'+book.id, oldPage);
      if(marks !== null) store.set('book.marks.'+book.id, marks);
    }''', {'book': book, 'offset': offset, 'oldPage': old_page, 'marks': marks})


def sample_book():
    text = ''.join(f'Párrafo {i}: Una lectura tranquila con palabras y signos.\n\n' for i in range(220))
    return {'id': 'fixture:read', 'title': 'Un jardín de papel', 'authors': ['Autora sintética'],
            'source': 'Fixture', 'source_format': 'txt', 'text': text, 'downloaded': 123}


@pytest.mark.parametrize('engine,width,height', [('chromium', 320, 640), ('webkit', 390, 844), ('chromium', 1280, 900)])
def test_reader_controls_never_cover_text_and_settings_persist(engine, width, height):
    book = sample_book()
    with browser_fixture('es', {'width': width, 'height': height}, engine=engine) as (page, errors):
        seed(page, book, old_page=1)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.locator('.book-page-label').first.inner_text().startswith('Parte 2 /')
        assert 'Copia de texto' in page.locator('.book-format-note').inner_text()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.locator('summary').get_by_text('Ajustes de lectura', exact=True).click()
        page.get_by_label('Tamaño de letra').fill('24')
        page.get_by_label('Fondo').select_option('night')
        page.get_by_label('Tipografía').select_option('sans')
        assert page.locator('.book-reader').get_attribute('data-theme') == 'night'
        assert page.locator('.book-reading').evaluate('n=>getComputedStyle(n).fontSize') == '24px'
        # Neither the action controls nor navigation uses sticky/fixed positioning.
        assert page.locator('.book-nav, .book-reader-actions, .book-disclosure').evaluate_all(
            "nodes=>nodes.every(n=>!['fixed','sticky'].includes(getComputedStyle(n).position))")
        page.locator('.book-reading').scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        assert page.locator('#scroll-discovery').is_hidden()
        assert page.locator('#view').evaluate('n=>n.scrollWidth <= n.clientWidth + 1')
        assert page.locator('.book-reading').evaluate('n=>n.getBoundingClientRect().width > 150')
        page.get_by_role('button', name='Marcar lugar', exact=True).click()
        assert page.evaluate("store.get('book.marks.fixture:read',[]).length") == 1
        page.get_by_role('button', name='Mi biblioteca', exact=True).click()
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.locator('.book-reader').get_attribute('data-theme') == 'night'
        assert page.locator('.book-reading').evaluate('n=>getComputedStyle(n).fontSize') == '24px'
        assert page.locator('.book-page-label').first.inner_text().startswith('Parte 2 /')
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_epub_search_download_reflows_heading_emphasis_and_image(monkeypatch, engine):
    picture = io.BytesIO()
    Image.new('RGB', (32, 16), '#669977').save(picture, 'PNG')
    raw = fixture_epub(['<body><h1>Capítulo 🎉</h1><p>Una historia <em>hermosa</em>.</p>'
                        '<img src="p.png" alt="Ilustración"><p>Fin.</p><script>bad()</script></body>'],
                       {'OEBPS/p.png': picture.getvalue()})
    monkeypatch.setattr(book_library, '_fetch', lambda *args: (raw, 'application/epub+zip'))
    monkeypatch.setattr(book_library, '_search', lambda *_args: ([{
        'id': 'gutenberg:fixture', 'source': 'Project Gutenberg', 'title': 'EPUB real sintético', 'authors': ['Autora'],
        'url': 'https://example.org/record',
        'downloads': [{'format': 'txt', 'url': 'https://example.org/b.txt'}, {'format': 'epub', 'url': 'https://example.org/b.epub'}]}], False))
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, delivery_handler=_handler) as (page, errors):
        page.evaluate(BRIDGE)
        page.evaluate("store.set('books.source','gutenberg');go('books',{q:'fixture'})")
        page.get_by_role('heading', name='EPUB real sintético', exact=True).wait_for()
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Abrir', exact=True).click(timeout=30000)
        assert page.evaluate("window.libraryRequests.find(x=>x.do==='download').format") == 'epub'
        assert page.locator('.book-reading h1').inner_text() == 'Capítulo 🎉'
        assert page.locator('.book-reading em').inner_text() == 'hermosa'
        assert page.locator('.book-reading img').get_attribute('alt') == 'Ilustración'
        assert page.locator('.book-reading img').get_attribute('src').startswith('data:image/jpeg;base64,')
        assert 'bad()' not in page.locator('.book-reading').inner_text()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.locator('summary').get_by_text('Índice', exact=True).click()
        assert page.locator('.book-contents').get_by_role('button', name='Capítulo 🎉').count() == 1
        page.reload()
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.locator('.book-reading img').count() == 1 and errors == []


def test_search_error_is_not_misdiagnosed_as_source_and_paging_retry_does_not_skip():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''()=>{const original=bridge.call.bind(bridge);window.__pages=[];window.__failed=false;
          bridge.call=async(op,args)=>{
            if(op!=='books')return original(op,args);
            window.__pages.push(args.page);
            if(args.page===2&&!window.__failed){window.__failed=true;throw Error('transport');}
            return {books:[{id:'f:'+args.page,title:'Resultado '+args.page,authors:[],source:'Fixture',
              url:'https://example.org',downloads:[{format:'txt',url:'https://example.org/b.txt'}]}],
              more:args.page===1, unavailable:args.page===1?['Google Books (límite temporal)']:[]};
          };go('books',{q:'un libro'});}''')
        page.get_by_role('heading', name='Resultado 1').wait_for()
        assert 'Google Books (límite temporal)' in page.get_by_role('status').inner_text()
        more = page.get_by_role('button', name='Más libros', exact=True)
        more.click()
        page.get_by_role('status').get_by_text('respuesta del bot', exact=False).wait_for()
        assert page.get_by_role('heading', name='Resultado 1').is_visible()
        assert 'La fuente no respondió' not in page.get_by_role('status').inner_text()
        more.click()
        page.get_by_role('heading', name='Resultado 2').wait_for()
        assert page.evaluate('window.__pages') == [1, 2, 2] and errors == []


def test_backup_round_trip_is_local_non_overwriting_and_keeps_progress():
    book = sample_book()
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book, offset=5000, marks=[{'offset': 4000, 'label': 'Mi lugar'}])
        page.evaluate("go('books',{tab:'saved'})")
        assert page.get_by_text('no está cifrada', exact=False).is_visible()
        page.evaluate("() => { window.exportSheet = async (blob,name)=>{window.__libraryExport={name,content:await blob.text()}}; }")
        before = page.evaluate('window.__sentUpdates.length')
        page.get_by_role('button', name='Copia de biblioteca', exact=True).click()
        page.wait_for_function('window.__libraryExport && window.__libraryExport.content')
        payload = json.loads(page.evaluate('window.__libraryExport.content'))
        assert payload['type'] == 'michiflin-library' and payload['books'][0]['progress']['offset'] == 5000
        assert payload['books'][0]['progress']['marks'][0]['label'] == 'Mi lugar'
        assert page.evaluate('window.__sentUpdates.length') == before
        # Simulate an empty *separate* instance by clearing only the synthetic
        # library. This does not access or modify a real Delta Chat message.
        page.evaluate('''async()=>{const db=await new Promise(r=>{let q=indexedDB.open('michiflin-reading-library-v1',1);q.onsuccess=()=>r(q.result)});
          await new Promise(r=>{let tx=db.transaction('books','readwrite');tx.objectStore('books').clear();tx.oncomplete=r});db.close();
          store.del('book.offset.fixture:read');store.del('book.marks.fixture:read');store.set('unrelated.fixture','kept');go('books',{tab:'saved'});}''')
        file = {'name': 'backup.json', 'mimeType': 'application/json', 'buffer': json.dumps(payload, ensure_ascii=False).encode()}
        page.locator('.book-file-input').set_input_files(file)
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for()
        assert page.evaluate("store.get('book.offset.fixture:read')") == 5000
        assert page.evaluate("store.get('book.marks.fixture:read')[0].label") == 'Mi lugar'
        assert page.evaluate("store.get('unrelated.fixture')") == 'kept'
        page.evaluate("store.set('book.offset.fixture:read',7777)")
        page.locator('.book-file-input').set_input_files(file)
        page.get_by_role('status').get_by_text('Ya existentes sin modificar: 1', exact=False).wait_for()
        assert page.evaluate("store.get('book.offset.fixture:read')") == 7777
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 1
        assert errors == []


def test_legacy_txt_import_and_invalid_multi_file_are_all_or_nothing():
    with browser_fixture('es', {'width': 320, 'height': 640}) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        old = {'name': 'viejo.txt', 'mimeType': 'text/plain', 'buffer': 'Título antiguo\nAutora\n\nTexto ñ 🎉'.encode()}
        broken = {'name': 'fake.json', 'mimeType': 'application/json', 'buffer': b'{"type":"not-a-backup"}'}
        page.locator('.book-file-input').set_input_files([old, broken])
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for()
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0
        page.locator('.book-file-input').set_input_files(old)
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for()
        assert page.evaluate('(async()=> (await BookShelf.all())[0].text)()') == 'Título antiguo\nAutora\n\nTexto ñ 🎉'
        page.locator('.book-file-input').set_input_files(old)
        page.get_by_role('status').get_by_text('Ya existentes sin modificar: 1', exact=False).wait_for()
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 1
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert 'Texto ñ 🎉' in page.locator('.book-reading').inner_text()
        assert '0%' in page.locator('.book-page-label').first.inner_text()
        assert errors == []


def test_imported_markup_and_remote_images_cannot_execute_or_hide_text():
    book = {'id': 'malicious:1', 'title': '<img src=x onerror=alert(1)>', 'text': 'El texto completo', 'authors': [],
            'presentation': [{'kind': 'image', 'at': 0, 'data': 'https://evil.example/x.png'},
                             {'kind': 'p', 'start': 3, 'end': 6}], 'source': 'test'}
    payload = {'type': 'michiflin-library', 'version': 1, 'books': [{'book': book, 'progress': {'offset': 0, 'marks': []}}]}
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'evil.json', 'mimeType': 'application/json', 'buffer': json.dumps(payload).encode()})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for()
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.locator('.book-reading').inner_text() == 'El texto completo'
        assert page.locator('.book-reading img').count() == 0
        assert page.locator('.book-reader-head img').count() == 0
        assert page.locator('.book-reader-head h2').inner_text() == book['title']
        assert errors == []


def test_reader_find_is_offline_and_jumps_to_later_part():
    book = sample_book()
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        before = page.evaluate('window.__sentUpdates.length')
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.locator('summary').get_by_text('Buscar en el libro', exact=True).click()
        page.get_by_label('Buscar dentro del libro').fill('Párrafo 200:')
        page.get_by_role('button', name='Buscar siguiente', exact=True).click()
        assert 'Párrafo 200:' in page.locator('.book-reading').inner_text()
        assert not page.locator('.book-page-label').first.inner_text().startswith('Parte 1 /')
        assert page.evaluate('window.__sentUpdates.length') == before
        assert errors == []


def test_bookmark_retains_reading_position_when_toolbar_is_revisited():
    book = sample_book()
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('.book-reading p').nth(15).scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        before = page.evaluate("store.get('book.offset.fixture:read')")
        assert before > 100
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.get_by_role('button', name='Marcar lugar', exact=True).click()
        marked = page.evaluate("store.get('book.marks.fixture:read',[])[0].offset")
        assert abs(marked - before) < 150
        assert errors == []


def test_storage_failure_does_not_claim_progress_was_saved():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, sample_book())
        page.evaluate('''()=>{const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key.startsWith('mf.book.offset.'))throw Error('quota');return original.call(this,key,value)};
          go('books',{tab:'saved'});}''')
        page.get_by_role('button', name='Abrir', exact=True).click()
        assert page.get_by_role('status').get_by_text('No se pudo guardar el progreso local', exact=False).is_visible()
        assert page.locator('.book-reading').is_visible() and errors == []


def test_import_capacity_limit_is_atomic():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''async()=>{const db=await new Promise((resolve,reject)=>{const q=indexedDB.open('michiflin-reading-library-v1',1);
          q.onupgradeneeded=()=>q.result.createObjectStore('books',{keyPath:'id'});q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          await new Promise((resolve,reject)=>{const tx=db.transaction('books','readwrite'),os=tx.objectStore('books');
            for(let i=0;i<40;i++)os.put({id:'full:'+i,title:'Libro '+i,text:'Texto guardado',authors:[],downloaded:i});
            tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error)});db.close();go('books',{tab:'saved'});}''')
        page.get_by_role('button', name='Importar TXT o copia', exact=True).wait_for()
        page.locator('.book-file-input').set_input_files({'name': 'extra.txt', 'mimeType': 'text/plain', 'buffer': b'extra'})
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for()
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 40
        assert page.evaluate("(async()=> !(await BookShelf.all()).some(b=>b.text==='extra'))()")
        assert errors == []


def test_concurrent_downloads_cannot_exceed_library_capacity():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''async()=>{const db=await new Promise((resolve,reject)=>{const q=indexedDB.open('michiflin-reading-library-v1',1);
          q.onupgradeneeded=()=>q.result.createObjectStore('books',{keyPath:'id'});q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          await new Promise((resolve,reject)=>{const tx=db.transaction('books','readwrite'),os=tx.objectStore('books');
            for(let i=0;i<39;i++)os.put({id:'full:'+i,title:'Libro '+i,text:'Texto guardado',authors:[]});
            tx.oncomplete=resolve;tx.onabort=()=>reject(tx.error)});db.close();
          const orig=bridge.call.bind(bridge);window.__releaseBooks=[];
          bridge.call=(op,args)=>{if(op!=='books')return orig(op,args);
            if(args.do==='search')return Promise.resolve({books:[1,2].map(i=>({id:'new:'+i,title:'Nuevo '+i,authors:[],source:'Fixture',
              url:'https://example.org/book',downloads:[{format:'txt',url:'https://example.org/'+i+'.txt'}]})),more:false});
            if(args.do==='download')return new Promise(resolve=>window.__releaseBooks.push(()=>resolve({text:'Texto nuevo',complete:true,source_format:'txt'})));
            return Promise.resolve({});};go('books',{q:'dos'});}''')
        page.get_by_role('heading', name='Nuevo 1').wait_for()
        page.get_by_role('button', name='Descargar', exact=True).nth(0).click()
        page.get_by_role('button', name='Descargar', exact=True).nth(1).click()
        page.wait_for_function('window.__releaseBooks.length === 2')
        page.evaluate('window.__releaseBooks.forEach(f=>f())')
        page.wait_for_function("document.querySelectorAll('.book-card .btn.primary:not([disabled])').length === 2")
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 40
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 1
        assert 'Límite de 40 libros' in page.get_by_role('status').inner_text()
        assert errors == []


def test_slow_source_offers_an_explicit_retry_instead_of_a_blank_screen():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''()=>{const orig=bridge.call.bind(bridge);window.__pages=[];
          bridge.call=(op,args)=>{if(op!=='books')return orig(op,args);window.__pages.push(args.page);
            if(args.page===1)return Promise.resolve({books:[],more:true,unavailable:['Project Gutenberg (tiempo de espera agotado)']});
            return Promise.resolve({books:[{id:'late:1',title:'Resultado tardío',authors:[],source:'Fixture',
              url:'https://example.org',downloads:[{format:'txt',url:'https://example.org/b.txt'}]}],more:false,unavailable:[]});};
          go('books',{q:'consulta lenta'});}''')
        page.get_by_role('status').get_by_text('tiempo de espera agotado', exact=False).wait_for()
        page.get_by_role('button', name='Reintentar fuentes', exact=True).click()
        page.get_by_role('heading', name='Resultado tardío').wait_for()
        assert page.evaluate('window.__pages') == [1, 2] and errors == []


def test_saved_shelf_and_reader_never_fetch_a_missing_cover_from_bot():
    book = {**sample_book(), 'cover_url': 'https://covers.openlibrary.org/b/id/123-L.jpg'}
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book)
        page.evaluate('''()=>{const original=bridge.call.bind(bridge);window.__bookCalls=[];
          bridge.call=(op,args)=>{if(op==='books'){window.__bookCalls.push(args);return Promise.resolve({})}return original(op,args)};
          go('books',{tab:'saved'});}''')
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.wait_for_timeout(500)
        assert page.evaluate('window.__bookCalls') == [] and errors == []


def test_find_treats_punctuation_as_literal_and_unicode_offsets_remain_correct():
    content = 'İ' * 50 + '\n\n' + 'Un título con [signos]+ y paréntesis (seguros).\n\n' + 'final.' * 800
    book = {'id': 'fixture:punctuation', 'title': 'Texto con signos', 'text': content, 'authors': []}
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book)
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.locator('summary').get_by_text('Buscar en el libro', exact=True).click()
        page.get_by_label('Buscar dentro del libro').fill('[signos]+')
        page.get_by_role('button', name='Buscar siguiente', exact=True).click()
        assert page.evaluate("store.get('book.offset.fixture:punctuation')") == content.index('[signos]+')
        assert errors == []


def test_changed_query_cannot_display_a_late_result_from_previous_words():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''()=>{const orig=bridge.call.bind(bridge);
          bridge.call=(op,args)=>{if(op!=='books')return orig(op,args);
            if(args.q==='primera')return new Promise(resolve=>window.__oldReply=()=>resolve({books:[{id:'old',title:'Resultado antiguo',authors:[],source:'Fixture',downloads:[{format:'txt',url:'https://example.org/a.txt'}]}],more:false}));
            return Promise.resolve({books:[{id:'new',title:'Resultado nuevo',authors:[],source:'Fixture',downloads:[{format:'txt',url:'https://example.org/b.txt'}]}],more:false});};
          go('books',{q:'primera'});}''')
        page.wait_for_function('typeof window.__oldReply === "function"')
        page.get_by_label('Título, autor o tema').fill('segunda')
        page.evaluate('window.__oldReply()')
        page.wait_for_timeout(100)
        assert page.get_by_role('heading', name='Resultado antiguo').count() == 0
        page.get_by_role('button', name='Buscar', exact=True).click()
        page.get_by_role('heading', name='Resultado nuevo').wait_for()
        assert errors == []


def test_read_aloud_uses_only_voices_declared_local():
    book = {**sample_book(), 'languages': ['spa']}
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        seed(page, book)
        page.evaluate('''()=>{window.__spoken=[];window.__voices=[{lang:'es-ES',localService:false}];
          Object.defineProperty(window,'speechSynthesis',{configurable:true,value:{cancel(){},getVoices(){return window.__voices},speak(u){window.__spoken.push(u)}}});
          window.SpeechSynthesisUtterance=function(value){this.text=value};go('books',{tab:'saved'});}''')
        page.get_by_role('button', name='Abrir', exact=True).click()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        page.get_by_role('button', name='Leer en voz alta', exact=True).click()
        assert page.evaluate('window.__spoken.length') == 0
        assert page.get_by_text('No hay una voz local disponible', exact=False).is_visible()
        page.evaluate("window.__voices.push({lang:'es-ES',localService:true})")
        page.get_by_role('button', name='Leer en voz alta', exact=True).click()
        assert page.evaluate('window.__spoken.length') == 1
        assert page.evaluate('window.__spoken[0].voice.localService') is True
        assert errors == []
