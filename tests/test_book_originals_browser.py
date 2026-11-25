"""Original-byte reader/backup checks in synthetic isolated browser instances."""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from tests.test_book_originals import sample_epub, sample_pdf
from tests.book_reader_harness import open_test_reader
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_original_epub_import_read_sandbox_and_exact_export(engine):
    raw = sample_epub('<h1>Capítulo uno</h1><p>Texto auténtico con <em>énfasis</em>.</p>'
                      '<img src="image.png" alt="Dibujo"/><img src="https://remote.invalid/tracker"/>'
                      '<script>parent.__book_attack = true</script><p onclick="parent.__book_attack = true">Seguro.</p>')
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        remote = []
        def blocked_route(route):
            remote.append(route.request.url)
            route.abort()
        page.route('**://remote.invalid/**', blocked_route)
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'example.epub', 'mimeType': 'application/epub+zip', 'buffer': raw})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
        stored = page.evaluate("""async () => { const book = (await BookShelf.all())[0]; return {
          format:book.original_format, hash:book.original_sha256, size:book.original.size, name:book.original_name }; }""")
        assert stored == {'format': 'epub', 'hash': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'name': 'example.epub'}
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        iframe = page.frame_locator('.book-file-stage iframe').first
        assert iframe.locator('h1').inner_text() == 'Capítulo uno'
        assert iframe.locator('em').inner_text() == 'énfasis'
        assert iframe.locator('script').count() == 0
        assert iframe.locator('img[alt="Dibujo"]').count() == 1
        assert iframe.locator('img[alt="Dibujo"]').evaluate('el=>el.complete && el.naturalWidth > 0')
        assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
        assert not page.evaluate('window.__book_attack || false')
        assert not remote
        page.evaluate('''() => { window.exportSheet = async (blob,name) => {
          const bytes=new Uint8Array(await blob.arrayBuffer()); let s=''; for(const b of bytes)s+=String.fromCharCode(b);
          window.__export={name,data:btoa(s)};
        }; }''')
        page.get_by_role('button', name='Exportar archivo original', exact=True).click()
        page.wait_for_function('window.__export')
        assert page.evaluate('window.__export.name') == 'example.epub'
        assert base64.b64decode(page.evaluate('window.__export.data')) == raw
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_original_pdf_import_pages_and_exact_export(engine):
    raw = sample_pdf()
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'example.pdf', 'mimeType': 'application/pdf', 'buffer': raw})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert page.locator('.book-pdf-canvas').evaluate('el=>el.width > 0 && el.height > 0')
        assert 'Página 1 / 3' in page.locator('.book-file-page').inner_text()
        page.get_by_role('button', name='Siguiente', exact=True).click()
        page.get_by_text('Página 2 / 3', exact=True).wait_for()
        page.evaluate('''() => { window.exportSheet = async (blob,name) => {
          const bytes=new Uint8Array(await blob.arrayBuffer()); let s=''; for(const b of bytes)s+=String.fromCharCode(b);
          window.__export={name,data:btoa(s)};
        }; }''')
        page.get_by_role('button', name='Exportar archivo original', exact=True).click()
        page.wait_for_function('window.__export')
        assert page.evaluate('window.__export.name') == 'example.pdf'
        assert base64.b64decode(page.evaluate('window.__export.data')) == raw
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_backup_round_trip_restores_exact_originals_and_pdf_progress(engine):
    epub, pdf, txt = sample_epub(), sample_pdf(), b'\xef\xbb\xbfA real TXT\r\nwith exact bytes.\r\n'
    files = [{'name': 'cuento.epub', 'mimeType': 'application/epub+zip', 'buffer': epub},
             {'name': 'paginas.pdf', 'mimeType': 'application/pdf', 'buffer': pdf},
             {'name': 'notas.txt', 'mimeType': 'text/plain', 'buffer': txt}]
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files(files)
        page.get_by_role('status').get_by_text('Importados: 3', exact=False).wait_for(timeout=30000)
        open_test_reader(page, 'paginas')
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        page.get_by_role('button', name='Siguiente', exact=True).click()
        page.get_by_text('Página 2 / 3', exact=True).wait_for()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=True).click()
        page.locator('summary').get_by_text('Marcadores', exact=True).click()
        page.get_by_role('button', name='Marcar lugar', exact=True).click()
        page.get_by_role('button', name='Mi biblioteca', exact=True).click()
        page.evaluate('''() => { window.exportSheet = async (blob,name) => {window.__backup={name,text:await blob.text()}}; }''')
        page.get_by_role('button', name='Copia de biblioteca', exact=True).click()
        page.wait_for_function('window.__backup && window.__backup.text')
        payload = json.loads(page.evaluate('window.__backup.text'))
        assert payload['version'] == 2 and len(payload['books']) == 3
        by_name = {item['book']['original_name']: item for item in payload['books']}
        for name, original in [('cuento.epub', epub), ('paginas.pdf', pdf), ('notas.txt', txt)]:
            assert base64.b64decode(by_name[name]['original_base64']) == original
            assert by_name[name]['book']['original_sha256'] == hashlib.sha256(original).hexdigest()
        assert by_name['paginas.pdf']['progress']['filePosition'] == 2
        assert by_name['paginas.pdf']['progress']['fileMarks'][0]['page'] == 2
        page.evaluate('''async()=>{const all=await BookShelf.all();const db=await new Promise(r=>{const q=indexedDB.open('michiflin-reading-library-v1');q.onsuccess=()=>r(q.result)});
          await new Promise(r=>{const tx=db.transaction(['books','originals'],'readwrite');tx.objectStore('books').clear();tx.objectStore('originals').clear();tx.oncomplete=r});db.close();
          for(const b of all){store.del('book.filePosition.'+b.id);store.del('book.fileMarks.'+b.id);}go('books',{tab:'saved'});}''')
        # A modified backup must fail atomically rather than saving anything.
        bad = json.loads(json.dumps(payload)); bad['books'][0]['book']['original_sha256'] = '0' * 64
        page.locator('.book-file-input').set_input_files({'name': 'bad.json', 'mimeType': 'application/json', 'buffer': json.dumps(bad).encode()})
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for(timeout=30000)
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0
        page.locator('.book-file-input').set_input_files({'name': 'good.json', 'mimeType': 'application/json', 'buffer': json.dumps(payload).encode()})
        page.get_by_role('status').get_by_text('Importados: 3', exact=False).wait_for(timeout=30000)
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 3
        for name, original in [('cuento.epub', epub), ('paginas.pdf', pdf), ('notas.txt', txt)]:
            actual = page.evaluate('''async(name)=>{const b=(await BookShelf.all()).find(x=>x.original_name===name);
              const bytes=new Uint8Array(await b.original.arrayBuffer());let s='';for(const x of bytes)s+=String.fromCharCode(x);return btoa(s);}''', name)
            assert base64.b64decode(actual) == original
        open_test_reader(page, 'paginas')
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert 'Página 2 / 3' in page.locator('.book-file-page').inner_text()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=True).click()
        page.locator('summary').get_by_text('Marcadores', exact=True).click()
        assert page.get_by_role('button', name='Página 2', exact=True).is_visible()
        assert errors == []


def test_legacy_text_is_honestly_labelled_and_cannot_recreate_original():
    payload = {'type': 'michiflin-library', 'version': 1, 'books': [{'book': {
        'id': 'legacy:1', 'title': 'Anterior', 'authors': ['Alguien'], 'source_format': 'epub',
        'text': 'Solo queda este texto.'}, 'progress': {'offset': 0, 'marks': []}}]}
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'old.json', 'mimeType': 'application/json', 'buffer': json.dumps(payload).encode()})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for()
        assert page.locator('.book-card .tag').inner_text().startswith('Copia de texto')
        open_test_reader(page)
        assert 'no está disponible' in page.locator('.book-format-note').inner_text() or 'no conserva el archivo original' in page.locator('.book-format-note').inner_text()
        page.locator('summary').get_by_text('Herramientas de lectura', exact=False).click()
        assert page.get_by_role('button', name='Exportar copia de texto (no original)', exact=True).is_visible()
        assert errors == []


def test_epub_with_encryption_metadata_remains_exportable_but_not_decrypted():
    raw = sample_epub(extra={'META-INF/encryption.xml': b'<encryption/>'})
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'locked.epub', 'mimeType': 'application/epub+zip', 'buffer': raw})
        page.get_by_role('status').get_by_text('fuentes ofuscadas', exact=False).wait_for(timeout=30000)
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('No se eludió ninguna protección', exact=False).wait_for(timeout=30000)
        assert page.locator('.book-file-stage iframe').count() == 0
        assert page.evaluate('(async()=> (await BookShelf.all())[0].original_sha256)()') == hashlib.sha256(raw).hexdigest()
        page.evaluate('''() => { window.exportSheet = async (blob,name) => {
          const bytes=new Uint8Array(await blob.arrayBuffer()); let s=''; for(const b of bytes)s+=String.fromCharCode(b);
          window.__export={name,data:btoa(s)};
        }; }''')
        page.get_by_role('button', name='Exportar archivo original', exact=True).click()
        page.wait_for_function('window.__export')
        assert base64.b64decode(page.evaluate('window.__export.data')) == raw
        assert errors == []


def test_worker_rejects_bad_crc_paths_and_timeout_without_hanging():
    import io
    import struct
    import zipfile
    raw = sample_epub()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        info = z.getinfo('mimetype')
        offset = info.header_offset
    broken = bytearray(raw)
    name_len, extra_len = struct.unpack_from('<HH', broken, offset + 26)
    broken[offset + 30 + name_len + extra_len] ^= 1
    traversal = sample_epub(extra={'../escape': b'data'})
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        for value in (bytes(broken), traversal):
            result = page.evaluate('''async(data)=>{const bytes=Uint8Array.from(atob(data),x=>x.charCodeAt(0));
              try{await BookOriginal.safeEpub(new Blob([bytes]));return 'accepted';}catch(e){return e.message;}}''', base64.b64encode(value).decode())
            assert result == 'structure'
        timed = page.evaluate('''async(data)=>{
          const NativeWorker=window.Worker, nativeSetTimeout=window.setTimeout;let stopped=false;
          window.Worker=class {postMessage(){} terminate(){stopped=true;}};
          window.setTimeout=(fn,ms,...args)=>nativeSetTimeout(fn,ms===30000?25:ms,...args);
          try{const bytes=Uint8Array.from(atob(data),x=>x.charCodeAt(0));await BookOriginal.safeEpub(new Blob([bytes]));return 'accepted';}
          catch(e){return e.message+':'+stopped;}finally{window.Worker=NativeWorker;window.setTimeout=nativeSetTimeout;}
        }''', base64.b64encode(raw).decode())
        assert timed == 'timeout:true'
        assert errors == []


def test_worker_rejects_expansion_zip64_and_entry_count_limits():
    import io
    import struct
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('META-INF/container.xml', b'<container/>')
        archive.writestr('bomb.txt', b'x' * 24_000_001)
    bomb = output.getvalue()
    assert len(bomb) < 100_000
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('META-INF/container.xml', b'<container/>')
        for i in range(3000):
            archive.writestr(f'entry-{i}', b'')
    too_many = output.getvalue()
    zip64 = bytearray(sample_epub())
    central = zip64.index(b'PK\x01\x02')
    struct.pack_into('<I', zip64, central + 20, 0xffffffff)
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        for value, expected in [(bomb, 'size'), (too_many, 'structure'), (bytes(zip64), 'structure')]:
            result = page.evaluate('''async(data)=>{const bytes=Uint8Array.from(atob(data),x=>x.charCodeAt(0));
              try{await BookOriginal.safeEpub(new Blob([bytes]));return 'accepted';}catch(e){return e.message;}}''', base64.b64encode(value).decode())
            assert result == expected
        assert errors == []
