"""Synthetic UI → dispatcher → original transfer → chunker → UI round trip.

The WebXDC transport itself is emulated; this is not a mail-delivery or native
Delta Chat test. Every address, binding and book is synthetic.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from collections import defaultdict

import pytest

from michiflin import access, admin, book_legacy, book_library, book_original, chat, config, miniapp, perf, state, usage
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_multichunk_original_through_dispatcher_and_bridge(monkeypatch, engine):
    raw = b'Original synthetic book data.\r\n' * 68000
    assert book_original.PART_BYTES * 2 < len(raw) < book_library.MAX_DOCUMENT
    scopes = []
    actual_start = book_original.start
    def start(scope, url, format):
        scopes.append(scope)
        return actual_start(scope, url, format)
    monkeypatch.setattr(book_original, 'start', start)
    monkeypatch.setattr(book_original, '_sessions', {})
    monkeypatch.setattr(book_legacy, '_sessions', {})
    monkeypatch.setattr(book_legacy, '_alternatives', {})
    monkeypatch.setattr(book_library, '_cache', {})
    monkeypatch.setattr(book_library, '_source_pause', {})
    monkeypatch.setattr(book_library, '_source_pause_reason', {})
    monkeypatch.setattr(book_library, '_fetch', lambda *_args, **_kwargs: (raw, 'text/plain'))
    monkeypatch.setattr(book_library, '_search', lambda *_args: ([{
        'id': 'archive:multi', 'source': 'archive', 'title': 'Libro de varias partes', 'authors': [],
        'url': 'https://example.invalid/record',
        'downloads': [{'format': 'txt', 'url': 'https://example.invalid/original.txt'}],
    }], False))
    monkeypatch.setattr(miniapp, '_answers', {})
    monkeypatch.setattr(miniapp, '_release_allows', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_status_allowed', lambda *_args: True)
    monkeypatch.setattr(miniapp, 'compatible_build', lambda *_args: True)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda *_args: True)
    monkeypatch.setattr(access, 'bound_allowed', lambda *_args: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda *_args: True)
    monkeypatch.setattr(admin, 'seen', lambda *_args: None)
    monkeypatch.setattr(config, 'service_access_allowed', lambda *_args: True)
    monkeypatch.setattr(state, 'maintenance_active', lambda: False)
    monkeypatch.setattr(state, 'maintenance_interrupted', lambda *_args: False)
    monkeypatch.setattr(state, 'get_kill_switch', lambda *_args: False)
    monkeypatch.setattr(state, 'kill_generation', lambda *_args: 0)
    monkeypatch.setattr(state, 'work_interrupted', lambda *_args: False)
    monkeypatch.setattr(state, 'guard_work', lambda *_args: contextlib.nullcontext())
    monkeypatch.setattr(chat, 'set_sender', lambda *_args: None)
    monkeypatch.setattr(chat, 'stop_capture', lambda: None)
    monkeypatch.setattr(perf, 'note_op', lambda *_args: None)
    monkeypatch.setattr(usage, 'note', lambda *_args, **_kwargs: None)
    updates = defaultdict(list)
    def send(_instance, payload, *_args, **_kwargs):
        updates[payload.get('re')].append(payload)
        return True
    monkeypatch.setattr(miniapp, '_send_status', send)
    entry = {'chat': 23, 'addr': 'owner@example.invalid', 'epoch': 'fixture', 'build': 'build', 'access_session': 'grant-1'}
    requests = []
    def handler(request):
        requests.append(request)
        # The client-supplied value must not determine the server transfer scope.
        request['args']['_book_scope'] = 'attacker-controlled'
        miniapp.handle(17, entry, request)
        return {'updates': updates.pop(request['id'], [])}
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, delivery_handler=handler) as (page, errors):
        page.evaluate('''() => {
          const native=window.webxdc.sendUpdate.bind(window.webxdc);
          window.__bookCompleted=[];window.__bookChunks=[];
          window.webxdc.sendUpdate=(update,info)=>{
            const request=update && update.payload;
            if(!request || request.t!=='req' || request.op!=='books')return native(update,info);
            fetch('/api/delivery',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)})
              .then(response=>response.json()).then(body=>{
                if(body.error)throw Error(body.error);
                for(const payload of body.data.updates){
                  if(payload.t==='chunk')window.__bookChunks.push({action:request.args.do,i:payload.i,n:payload.n,size:new Blob([JSON.stringify({payload})]).size});
                  window.__listener({serial:++window.__serial,payload});
                }
                window.__bookCompleted.push(request.args.do);
              }).catch(error=>{window.__fixtureError=String(error)});
          };
          store.set('books.source','archive');go('books',{q:'multi'});
        }''')
        page.get_by_role('heading', name='Libro de varias partes').wait_for(timeout=30000)
        page.get_by_role('button', name='Descargar', exact=True).click()
        page.get_by_role('button', name='Guardado', exact=True).wait_for(timeout=60000)
        page.wait_for_function("__bookCompleted.includes('original_close')", timeout=30000)
        expected_scope = hashlib.sha256(json.dumps([17, 23, 'owner@example.invalid', 'fixture', 'build', 'grant-1'], ensure_ascii=True).encode()).hexdigest()
        assert scopes == [expected_scope] and scopes[0] != 'attacker-controlled'
        actions = [request['args']['do'] for request in requests]
        assert actions.count('original_start') == 1
        assert actions.count('original_part') == 2
        assert actions.count('original_close') == 1
        assert not book_original._sessions
        chunks = page.evaluate('window.__bookChunks')
        assert sum(item['action'] == 'original_start' for item in chunks) > 50
        assert sum(item['action'] == 'original_part' for item in chunks) > 50
        assert all(item['size'] < 128000 for item in chunks)
        stored = page.evaluate('''async()=>{const b=(await BookShelf.all())[0];return {
          hash:b.original_sha256,size:b.original.size,textLength:b.text.length,format:b.original_format}}''')
        assert stored == {'hash': hashlib.sha256(raw).hexdigest(), 'size': len(raw), 'textLength': len(raw), 'format': 'txt'}
        assert page.get_by_role('button', name='Abrir', exact=True).count() == 0
        assert page.locator('.book-reading').count() == 0
        page.locator('[data-key=saved]').click()
        page.evaluate("() => { window.exportSheet=async(blob,name)=>{window.__fixtureExportHash=await FileKitCrypto.hashBlob(blob)}; }")
        page.get_by_role('button', name='Exportar original', exact=True).click()
        page.wait_for_function('window.__fixtureExportHash')
        assert page.evaluate('window.__fixtureExportHash') == hashlib.sha256(raw).hexdigest()
        assert not page.evaluate('window.__fixtureError || false') and errors == []
