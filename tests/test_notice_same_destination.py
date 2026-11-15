"""Unread replies sharing a destination must remain individually reachable."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


def count(page, op):
    return page.evaluate("op => window.__sentUpdates.filter(x => x.payload?.op === op).length", op)


def deliver(page, rid, body, ok=True):
    page.evaluate("""value => window.__listener({serial:bridge.serial+1,payload:{t:'res',re:value.id,
      ok:value.ok,data:value.ok?value.body:null,error:value.ok?'':value.body}})""", {'id': rid, 'ok': ok, 'body': body})


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_two_chat_responses_are_not_both_marked_read_at_once(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=60000;go('chat',{space:'agent'});ask('agent','Primera pregunta sintética');
          ask('agent','Segunda pregunta sintética');goTab('home')}""")
        rids = page.evaluate("Object.entries(bridge.pending()).filter(([,m])=>m.op==='ask').map(([id])=>id)")
        assert len(rids) == 2
        deliver(page, rids[0], {'text': 'Primera respuesta sintética. ' + ('Texto largo. ' * 100), 'actions': []})
        deliver(page, rids[1], {'text': 'Segunda respuesta sintética. ' + ('Otra línea. ' * 100), 'actions': []})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='chat').length===2")
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.get_by_text('Segunda respuesta sintética', exact=False).wait_for(state='visible')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='chat').length") == 1
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.target?.view==='chat').messageId") == rids[0]
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Primera respuesta sintética', exact=False).wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='chat')")
        # Opening the earlier notice brings its own message into the scroller,
        # instead of always jumping to the latest message.
        page.wait_for_function("""id=>{const e=Array.from(document.querySelectorAll('[data-message-id]')).find(n=>n.dataset.messageId===id);
          const main=document.querySelector('#view');if(!e)return false;const r=e.getBoundingClientRect(),m=main.getBoundingClientRect();
          return r.top<m.bottom && r.bottom>m.top;}""", arg=rids[0])
        assert count(page, 'ask') == 2
        assert errors == []


def test_two_gallery_errors_are_opened_individually():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=60000;go('images');createImage('Primera imagen sintética');
          createImage('Segunda imagen sintética');goTab('home')}""")
        rids = page.evaluate("Object.entries(bridge.pending()).filter(([,m])=>m.op==='image').map(([id])=>id)")
        assert len(rids) == 2
        deliver(page, rids[0], 'Error primero', ok=False)
        deliver(page, rids[1], 'Error segundo', ok=False)
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='images').length===2")
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.locator('.image-failure').get_by_text('Error segundo', exact=False).wait_for(state='visible')
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='images').length===1")
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.target?.view==='images').imageId") == rids[0]
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.locator('.image-failure').get_by_text('Error primero', exact=False).wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='images')")
        assert count(page, 'image') == 2
        assert errors == []


def test_same_search_key_preserves_both_unread_snapshots_across_reopen():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=60000;go('results',{q:'misma consulta',engine:'web',mode:'standard'});goTab('apps')}""")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, first, {'engine': 'web', 'results': [{'title': 'Versión anterior', 'url': 'https://example.test/old'}]})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length===1")
        # Deliberate second synthetic request with the same destination/key:
        # delayed and replayed updates must not replace the unread first one.
        second = page.evaluate("""() => {const n=store.get('notices',[]).find(x=>x.unread&&x.target?.view==='results');
          return bridge.request('search',{q:'misma consulta',engine:'web',mode:'standard'},
            {kind:'view-result',key:n.cacheKey,target:n.target,category:'web',label:'notification_search'}).id;}""")
        deliver(page, second, {'engine': 'web', 'results': [{'title': 'Versión reciente', 'url': 'https://example.test/new'}]})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length===2")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Versión reciente', exact=True).wait_for(state='visible')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 1
        # Reopening must still recover the first snapshot without another search.
        page.evaluate("store.set('tab','apps')")
        page.reload()
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Versión anterior', exact=True).wait_for(state='visible', timeout=5000)
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        assert count(page, 'search') == 0
        assert errors == []


def test_two_download_notices_are_opened_individually():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {for(const id of [11,12]) bridge.pushes.job({id,title:'Archivo '+id,status:'failed',kind:'file',
          error:'Error '+id,created:now(),parts:0});}""")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='downloads').length") == 2
        page.locator('.tab[data-tab="downloads"]:visible').click()
        page.get_by_text('Error 12', exact=True).wait_for(state='visible')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='downloads').length") == 1
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Error 11', exact=True).wait_for(state='visible')
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='downloads')")
        assert count(page, 'queue') == 0
        assert errors == []


@pytest.mark.parametrize('view,op,category', [
    ('results', 'search', 'web'),
    ('youtube', 'youtube_search', 'apps'),
])
def test_error_snapshot_remains_visible_when_result_storage_fails(view, op, category):
    """A cache write failure must not hide a received error or replace it with stale data."""
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {
          const set=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value) {
            if(key==='mf.resultCache') throw new DOMException('synthetic quota','QuotaExceededError');
            return set.call(this,key,value);
          };
          window.__delay=60000;
        }""")
        if view == 'results':
            page.evaluate("go('results',{q:'misma',engine:'web',mode:'standard'});goTab('home')")
            args = {'q': 'misma', 'engine': 'web', 'mode': 'standard'}
            data = {'engine': 'web', 'results': [{'title': 'Respuesta posterior', 'url': 'https://example.test/new'}]}
        else:
            page.evaluate("go('youtube',{q:'misma'});goTab('home')")
            args = {'q': 'misma', 'page': 1}
            data = {'results': [{'title': 'Respuesta posterior', 'url': 'https://youtube.com/watch?v=synthetic'}], 'more': False}
        first = page.evaluate('op=>Object.entries(bridge.pending()).find(([,m])=>m.op===op)[0]', op)
        deliver(page, first, 'Error sintético anterior', ok=False)
        page.wait_for_function('v=>store.get("notices",[]).some(n=>n.unread&&n.target?.view===v)', arg=view)
        second = page.evaluate("""v=>{const n=store.get('notices',[]).find(x=>x.unread&&x.target?.view===v.view);
          return bridge.request(v.op,v.args,{kind:'view-result',key:n.cacheKey,target:n.target,
            category:v.category,label:'notification_search'}).id;}""", {'view': view, 'op': op, 'args': args, 'category': category})
        deliver(page, second, data)
        assert page.evaluate('v=>store.get("notices",[]).filter(n=>n.unread&&n.target?.view===v).length', view) == 2
        # The failure is explicit; current-session memory can still recover both replies.
        assert page.get_by_text('El almacenamiento de la app está lleno', exact=False).count() >= 1
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        page.get_by_text('Respuesta posterior', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Error sintético anterior', exact=True).wait_for(state='visible')
        assert not page.evaluate('v=>store.get("notices",[]).some(n=>n.unread&&n.target?.view===v)', view)
        assert count(page, op) == 2
        assert errors == []


def test_old_saved_reply_is_not_overwritten_when_copy_fails_after_reload():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=60000;go('results',{q:'conservada',engine:'web',mode:'standard'});goTab('apps')")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, first, {'engine': 'web', 'results': [{'title': 'Respuesta persistida', 'url': 'https://example.test/old'}]})
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        page.reload()
        page.wait_for_function('!!window.__listener && cache.at > 0')
        assert page.evaluate('resultMemoTimes.size') == 0
        # Refuse only the attempted copy under the old notice's snapshot key;
        # allow the new, distinct response and the notices themselves to save.
        second = page.evaluate("""() => {window.__delay=60000;
          const n=store.get('notices',[]).find(x=>x.unread&&x.target?.view==='results');
          const blocked=JSON.stringify(['notice-snapshot-v1',n.cacheKey,n.id]);
          const set=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value) {
            if(key==='mf.resultCache' && Object.prototype.hasOwnProperty.call(JSON.parse(value),blocked))
              throw new DOMException('synthetic quota','QuotaExceededError');
            return set.call(this,key,value);
          };
          return bridge.request('search',{q:'conservada',engine:'web',mode:'standard'},
            {kind:'view-result',key:n.cacheKey,target:n.target,category:'web',label:'notification_search'}).id;
        }""")
        deliver(page, second, {'engine': 'web', 'results': [{'title': 'Respuesta nueva', 'url': 'https://example.test/new'}]})
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 2
        assert page.get_by_text('El almacenamiento de la app está lleno', exact=False).count() >= 1
        # Both actual results were preserved to disk; reopening clears the test's
        # temporary storage fault as well as the in-memory fallback.
        page.reload()
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Respuesta nueva', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Respuesta persistida', exact=True).wait_for(state='visible')
        assert count(page, 'search') == 0
        assert errors == []


def test_failed_notice_rewrite_does_not_replace_the_existing_reply():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=60000;go('results',{q:'mismo',engine:'web',mode:'standard'});goTab('apps')")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, first, {'engine': 'web', 'results': [{'title': 'Respuesta original', 'url': 'https://example.test/old'}]})
        second = page.evaluate("""() => {const n=store.get('notices',[]).find(x=>x.unread&&x.target?.view==='results');
          const set=Storage.prototype.setItem;let failOnce=true;
          Storage.prototype.setItem=function(key,value) {
            if(key==='mf.notices' && failOnce) {failOnce=false;throw new DOMException('synthetic quota','QuotaExceededError');}
            return set.call(this,key,value);
          };
          return bridge.request('search',{q:'mismo',engine:'web',mode:'standard'},
            {kind:'view-result',key:n.cacheKey,target:n.target,category:'web',label:'notification_search'}).id;
        }""")
        deliver(page, second, {'engine': 'web', 'results': [{'title': 'Respuesta distinta', 'url': 'https://example.test/new'}]})
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 2
        assert page.get_by_text('El almacenamiento de la app está lleno', exact=False).count() >= 1
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Respuesta distinta', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Respuesta original', exact=True).wait_for(state='visible')
        assert count(page, 'search') == 2
        assert errors == []


def test_oversize_results_are_temporary_and_expire_without_a_new_request():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        for query, title in [('grande-1', 'Grande anterior'), ('grande-2', 'Grande reciente')]:
            page.evaluate("q=>{window.__delay=60000;go('results',{q,engine:'web',mode:'standard'});goTab('apps')}", query)
            rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
            deliver(page, rid, {'engine': 'web', 'results': [{'title': title, 'url': 'https://example.test/large'}], 'padding': 'x' * 181000})
        assert page.evaluate('resultMemoTimes.size') == 2
        assert page.evaluate('Object.keys(savedResults()).length') == 0
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Grande reciente', exact=True).wait_for(state='visible')
        assert count(page, 'search') == 2
        page.evaluate("store.set('tab','apps')")
        page.reload()
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        assert count(page, 'search') == 0
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        assert errors == []


def test_seventh_result_evicts_only_oldest_and_reports_expiry():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        for index in range(7):
            page.evaluate("q=>{window.__delay=60000;go('results',{q,engine:'web',mode:'standard'});goTab('apps')}", f'consulta-{index}')
            rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
            deliver(page, rid, {'engine': 'web', 'results': [{'title': f'Resultado {index}', 'url': f'https://example.test/{index}'}]})
        assert page.evaluate('Object.keys(savedResults()).length') == 6
        assert page.evaluate('resultMemoTimes.size') == 6
        page.evaluate("openNotice(store.get('notices',[]).find(n=>n.target?.params?.q==='consulta-0'))")
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 6
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Resultado 6', exact=True).wait_for(state='visible')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 5
        assert count(page, 'search') == 7
        assert errors == []


def test_ambiguous_legacy_notices_do_not_show_the_wrong_reply():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=60000;go('results',{q:'legado',engine:'web',mode:'standard'});goTab('apps')")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, first, {'engine': 'web', 'results': [{'title': 'Resultado legado', 'url': 'https://example.test/old'}]})
        second = page.evaluate("""() => {const list=store.get('notices',[]),n=list.find(x=>x.unread&&x.target?.view==='results');
          list.push({...n,id:'synthetic-ambiguous-older',ts:n.ts-1});store.set('notices',list);
          return bridge.request('search',{q:'legado',engine:'web',mode:'standard'},
            {kind:'view-result',key:n.cacheKey,target:n.target,category:'web',label:'notification_search'}).id;
        }""")
        deliver(page, second, {'engine': 'web', 'results': [{'title': 'Resultado nuevo', 'url': 'https://example.test/new'}]})
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='results').length") == 3
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Resultado nuevo', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Ver respuestas pendientes (2)').click()
        page.get_by_text('Resultado legado', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        assert count(page, 'search') == 2
        assert errors == []
