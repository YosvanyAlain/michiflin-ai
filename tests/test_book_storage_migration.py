"""Synthetic IndexedDB upgrade, integrity and atomicity checks; no live data."""
from __future__ import annotations

import base64
import hashlib

import pytest

from tests.test_book_originals import sample_pdf
from tests.book_reader_harness import open_test_reader
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_v1_upgrade_preserves_text_and_inline_bytes_without_loading_originals_on_shelf(engine):
    raw = sample_pdf()
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate('''async ({pdf, hash}) => {
          const request = indexedDB.open('michiflin-reading-library-v1', 1);
          request.onupgradeneeded = () => request.result.createObjectStore('books', {keyPath:'id'});
          const db = await new Promise((resolve,reject)=>{request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error)});
          await new Promise((resolve,reject)=>{
            const tx=db.transaction('books','readwrite'), books=tx.objectStore('books');
            books.put({id:'legacy:text',title:'Copia anterior',authors:['Alguien'],source_format:'epub',text:'Texto antiguo conservado.'});
            const bytes=Uint8Array.from(atob(pdf),x=>x.charCodeAt(0));
            books.put({id:'legacy:pdf',title:'PDF anterior',authors:[],text:'',original_format:'pdf',
              original_sha256:hash,original_name:'anterior.pdf',original_bytes:bytes.buffer});
            tx.oncomplete=resolve;tx.onerror=tx.onabort=()=>reject(tx.error);
          });
          db.close();
          store.set('book.filePosition.legacy:pdf',2,true);
          const native=IDBObjectStore.prototype.get;
          window.__originalReads=0;
          IDBObjectStore.prototype.get=function(...args){if(this.name==='originals')window.__originalReads++;return native.apply(this,args)};
          go('books',{tab:'saved'});
        }''', {'pdf': base64.b64encode(raw).decode(), 'hash': hashlib.sha256(raw).hexdigest()})
        page.get_by_role('heading', name='PDF anterior').wait_for()
        page.get_by_role('heading', name='Copia anterior').wait_for()
        assert page.evaluate('window.__originalReads') == 0
        metadata = page.evaluate('''async()=>{
          const q=indexedDB.open('michiflin-reading-library-v1');
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          const result=await new Promise((resolve,reject)=>{
            const tx=db.transaction(['books','originals']), b=tx.objectStore('books').get('legacy:pdf'),
              o=tx.objectStore('originals').get('legacy:pdf'), text=tx.objectStore('books').get('legacy:text');
            tx.oncomplete=()=>resolve({version:db.version,meta:b.result,bytes:o.result.byteLength,text:text.result.text});
            tx.onabort=tx.onerror=()=>reject(tx.error);
          });db.close();return result;
        }''')
        assert metadata['version'] == 2
        assert metadata['bytes'] == len(raw)
        assert metadata['text'] == 'Texto antiguo conservado.'
        assert metadata['meta']['original_available'] is True
        assert 'original_bytes' not in metadata['meta']
        page.evaluate('window.__originalReads=0')
        open_test_reader(page, 'PDF anterior')
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert page.evaluate('window.__originalReads') == 1
        assert 'Página 2 / 3' in page.locator('.book-file-page').inner_text()
        page.get_by_role('button', name='Mi biblioteca', exact=True).click()
        open_test_reader(page, 'Copia anterior')
        assert page.locator('.book-reading').inner_text() == 'Texto antiguo conservado.'
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_corrupted_stored_original_is_not_opened_exported_or_included_in_backup(engine):
    raw = b'This is the original text.\r\n'
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files({'name': 'safe.txt', 'mimeType': 'text/plain', 'buffer': raw})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
        page.evaluate('''async()=>{
          const q=indexedDB.open('michiflin-reading-library-v1');
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          await new Promise((resolve,reject)=>{
            const tx=db.transaction('originals','readwrite');
            tx.objectStore('originals').put(new TextEncoder().encode('tampered').buffer,'import:' + '%s');
            tx.oncomplete=resolve;tx.onabort=tx.onerror=()=>reject(tx.error);
          });db.close();window.__exports=0;window.exportSheet=()=>{window.__exports++};
        }''' % hashlib.sha256(raw).hexdigest())
        open_test_reader(page)
        page.locator('#toasts').get_by_text('no supera la comprobación', exact=False).wait_for()
        assert page.locator('.book-reader').count() == 0
        page.get_by_role('button', name='Exportar original', exact=True).click()
        page.locator('#toasts').get_by_text('no supera la comprobación', exact=False).first.wait_for()
        page.get_by_role('button', name='Copia de biblioteca', exact=True).click()
        page.locator('#toasts').get_by_text('No se pudo preparar la copia', exact=False).wait_for()
        assert page.evaluate('window.__exports') == 0
        assert errors == []


def test_aborted_original_write_rolls_back_whole_import_transaction():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate('''()=>{
          go('books',{tab:'saved'});
          window.__nativePut=IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put=function(...args){
            if(this.name==='originals'){this.transaction.abort();return null;}
            return window.__nativePut.apply(this,args);
          };
        }''')
        page.locator('.book-file-input').set_input_files([
            {'name': 'one.txt', 'mimeType': 'text/plain', 'buffer': b'First original'},
            {'name': 'two.txt', 'mimeType': 'text/plain', 'buffer': b'Second original'},
        ])
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for(timeout=30000)
        page.evaluate('()=>{IDBObjectStore.prototype.put=window.__nativePut}')
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('failure', ['synchronous_exception', 'transaction_abort'])
def test_delete_rolls_back_metadata_and_original_on_storage_failure_and_retries(engine, failure):
    raw = b'\xef\xbb\xbfSynthetic original\r\nwith exact bytes.\n'
    identifier = 'import:' + hashlib.sha256(raw).hexdigest()
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        page.locator('.book-file-input').set_input_files(
            {'name': 'atomic.txt', 'mimeType': 'text/plain', 'buffer': raw})
        page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
        page.evaluate('''({id,failure})=>{
          store.set('book.offset.'+id, 7, true);
          window.__nativeDelete=IDBObjectStore.prototype.delete;
          IDBObjectStore.prototype.delete=function(...args){
            if(this.name==='originals'){
              if(failure==='synchronous_exception')throw Error('synthetic-storage-failure');
              this.transaction.abort();return null;
            }
            return window.__nativeDelete.apply(this,args);
          };
        }''', {'id': identifier, 'failure': failure})
        page.locator('.book-card').get_by_role('button', name='Eliminar', exact=True).click()
        page.locator('#sheet').get_by_role('button', name='Eliminar', exact=True).click()
        page.get_by_role('status').get_by_text('No se pudo eliminar la copia', exact=False).wait_for(timeout=30000)
        preserved = page.evaluate('''async(id)=>{
          IDBObjectStore.prototype.delete=window.__nativeDelete;
          const q=indexedDB.open('michiflin-reading-library-v1');
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          const result=await new Promise((resolve,reject)=>{
            const tx=db.transaction(['books','originals']), meta=tx.objectStore('books').get(id),
              bytes=tx.objectStore('originals').get(id);
            tx.oncomplete=()=>resolve({meta:!!meta.result, bytes:bytes.result ? Array.from(new Uint8Array(bytes.result)) : null,
              offset:store.get('book.offset.'+id,null)});
            tx.onabort=tx.onerror=()=>reject(tx.error);
          });db.close();return result;
        }''', identifier)
        assert preserved == {'meta': True, 'bytes': list(raw), 'offset': 7}
        assert page.locator('.book-card').count() == 1
        page.locator('.book-card').get_by_role('button', name='Eliminar', exact=True).click()
        page.locator('#sheet').get_by_role('button', name='Eliminar', exact=True).click()
        page.get_by_role('status').get_by_text('Copia eliminada', exact=False).wait_for(timeout=30000)
        removed = page.evaluate('''async(id)=>{
          const q=indexedDB.open('michiflin-reading-library-v1');
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          const result=await new Promise((resolve,reject)=>{
            const tx=db.transaction(['books','originals']), meta=tx.objectStore('books').get(id),
              bytes=tx.objectStore('originals').get(id);
            tx.oncomplete=()=>resolve({meta:!!meta.result,bytes:!!bytes.result,
              offset:store.get('book.offset.'+id,null)});
            tx.onabort=tx.onerror=()=>reject(tx.error);
          });db.close();return result;
        }''', identifier)
        assert removed == {'meta': False, 'bytes': False, 'offset': None}
        assert page.locator('.book-card').count() == 0
        assert errors == []


def test_limit_is_atomic_and_existing_originals_are_not_overwritten():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate("go('books',{tab:'saved'})")
        files = [{'name': f'book-{i}.txt', 'mimeType': 'text/plain', 'buffer': f'Synthetic book {i}'.encode()}
                 for i in range(40)]
        page.locator('.book-file-input').set_input_files(files)
        page.get_by_role('status').get_by_text('Importados: 40', exact=False).wait_for(timeout=30000)
        page.locator('.book-file-input').set_input_files({'name': 'extra.txt', 'mimeType': 'text/plain', 'buffer': b'Book 41'})
        page.get_by_role('status').get_by_text('No se importó nada', exact=False).wait_for(timeout=30000)
        assert page.evaluate('(async()=> (await BookShelf.all()).length)()') == 0 + 40
        assert page.locator('.book-card').count() == 40
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_failed_v1_upgrade_rolls_back_and_can_be_retried(engine):
    raw = sample_pdf()
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate('''async({data,hash})=>{
          const q=indexedDB.open('michiflin-reading-library-v1',1);
          q.onupgradeneeded=()=>q.result.createObjectStore('books',{keyPath:'id'});
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          await new Promise((resolve,reject)=>{
            const tx=db.transaction('books','readwrite');
            tx.objectStore('books').put({id:'legacy:pdf',title:'PDF antes del fallo',authors:[],text:'',
              original_format:'pdf',original_sha256:hash,original_name:'before.pdf',
              original_bytes:Uint8Array.from(atob(data),x=>x.charCodeAt(0)).buffer});
            tx.oncomplete=resolve;tx.onabort=tx.onerror=()=>reject(tx.error);
          });db.close();
          window.__nativePut=IDBObjectStore.prototype.put;
          IDBObjectStore.prototype.put=function(...args){
            if(this.name==='originals')throw Error('synthetic-storage-failure');
            return window.__nativePut.apply(this,args);
          };
          go('books',{tab:'saved'});
        }''', {'data': base64.b64encode(raw).decode(), 'hash': hashlib.sha256(raw).hexdigest()})
        page.get_by_role('status').get_by_text('El almacenamiento local no está disponible', exact=False).wait_for(timeout=30000)
        preserved = page.evaluate('''async()=>{
          IDBObjectStore.prototype.put=window.__nativePut;
          const q=indexedDB.open('michiflin-reading-library-v1',1);
          const db=await new Promise((resolve,reject)=>{q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error)});
          const record=await new Promise((resolve,reject)=>{
            const tx=db.transaction('books'),req=tx.objectStore('books').get('legacy:pdf');
            tx.oncomplete=()=>resolve(req.result);tx.onabort=tx.onerror=()=>reject(tx.error);
          });
          const bytes=new Uint8Array(record.original_bytes);let s='';for(const b of bytes)s+=String.fromCharCode(b);
          const value={version:db.version,hasOriginals:db.objectStoreNames.contains('originals'),data:btoa(s)};
          db.close();return value;
        }''')
        assert preserved['version'] == 1 and preserved['hasOriginals'] is False
        assert base64.b64decode(preserved['data']) == raw
        page.evaluate("go('books',{tab:'saved'})")
        page.get_by_role('heading', name='PDF antes del fallo').wait_for(timeout=30000)
        open_test_reader(page)
        page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
        assert errors == []
