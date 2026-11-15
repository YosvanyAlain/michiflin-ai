"""Results hidden by a sheet remain unread; bridge and data are synthetic."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


def deliver(page, rid, data, error=""):
    page.evaluate("""value => window.__listener({serial:bridge.serial+1,payload:{t:'res',
      re:value.id,ok:!value.error,data:value.error?null:value.data,error:value.error}})""",
                  {"id": rid, "data": data, "error": error})


def sent_count(page, op):
    return page.evaluate("op => window.__sentUpdates.filter(x => x.payload?.op === op).length", op)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_web_result_arriving_behind_sheet_remains_unread_until_sheet_closes(engine, failed):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;
          go('results',{q:'consulta cubierta',engine:'web',mode:'standard'});
          openSheet('Panel sintético',[h('p',{},'El resultado queda detrás')]);}""")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        data = {"engine": "web", "results": [{"title": "Resultado cubierto", "url": "https://example.test/covered"}]}
        deliver(page, rid, data, "Fallo sintético cubierto" if failed else "")
        page.wait_for_function("id=>!bridge.pending()[id]", arg=rid)
        assert page.evaluate("!document.querySelector('#sheet').hidden")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='consulta cubierta')")
        assert page.locator('.tab[data-tab="web"]:visible .tab-badge').inner_text() == "1"
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='consulta cubierta')")
        page.evaluate("closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='consulta cubierta')")
        page.get_by_text("Fallo sintético cubierto" if failed else "Resultado cubierto", exact=False).first.wait_for(state="visible")
        assert page.locator('.tab[data-tab="web"]:visible .tab-badge').is_hidden()
        assert sent_count(page, "search") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reopening_sheet_during_close_does_not_hide_the_new_sheet(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {openSheet('Primera hoja',[h('p',{},'Primera')]);closeSheet();
          openSheet('Segunda hoja',[h('p',{},'Segunda')]);}""")
        page.wait_for_timeout(350)
        assert page.evaluate("!document.querySelector('#sheet').hidden")
        assert page.locator("#sheet h2").inner_text() == "Segunda hoja"
        assert errors == []


VIEW_REQUESTS = [
    ("reader", "read", {"url": "https://example.test/article"}, "Contenido sintético del lector", "web"),
    ("transcript", "action", {"url": "https://youtube.com/watch?v=fixture"}, "smoke test result", "apps"),
    ("music_item", "music_item", {"id": "album-1"}, "Jazz track", "apps"),
]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("view,op,params,success,category", VIEW_REQUESTS)
def test_reader_transcript_and_music_wait_for_sheet_to_close(engine, failed, view, op, params, success, category):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""v => {window.__delay=400;if(v.failed)window.__failNext[v.op]=1;
          go(v.view,v.params);openSheet('Panel sintético',[h('p',{},'Cubierto')]);}""",
                      {"view": view, "op": op, "params": params, "failed": failed})
        page.wait_for_function("op=>window.__fixtureResponses.includes(op)", arg=op)
        page.wait_for_function("v=>store.get('notices',[]).some(n=>n.unread&&n.target?.view===v)", arg=view)
        assert page.locator(f'.tab[data-tab="{category}"]:visible .tab-badge').inner_text() == "1"
        assert page.evaluate("!document.querySelector('#sheet').hidden")
        page.evaluate("closeSheet()")
        page.wait_for_function("v=>document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.target?.view===v)", arg=view)
        page.get_by_text("SIMULATED network uncertainty" if failed else success, exact=False).first.wait_for(state="visible")
        assert sent_count(page, op) == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("reverse", [False, True])
def test_two_chat_replies_under_sheet_are_opened_individually(engine, reverse):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;go('chat',{space:'agent'});
          ask('agent','Pregunta uno');ask('agent','Pregunta dos');
          openSheet('Panel sintético',[h('p',{},'Cubierto')]);}""")
        rids = page.evaluate("Object.entries(bridge.pending()).filter(([,m])=>m.op==='ask').map(([id])=>id)")
        assert len(rids) == 2
        replies = [
            {"text": "Respuesta cubierta uno. " + "Texto largo. " * 100, "actions": []},
            {"text": "Respuesta cubierta dos. " + "Otra línea. " * 100, "actions": []},
        ]
        order = [1, 0] if reverse else [0, 1]
        for index in order:
            deliver(page, rids[index], replies[index])
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='chat').length===2")
        page.evaluate("closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden && store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='chat').length===1")
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.target?.view==='chat').messageId") == rids[order[0]]
        page.wait_for_function("""id=>{const e=Array.from(document.querySelectorAll('[data-message-id]')).find(n=>n.dataset.messageId===id);
          const m=document.querySelector('#view');if(!e)return false;const r=e.getBoundingClientRect(),b=m.getBoundingClientRect();
          return r.top<b.bottom && r.bottom>b.top;}""", arg=rids[order[1]])
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='chat')")
        page.wait_for_function("""id=>{const e=Array.from(document.querySelectorAll('[data-message-id]')).find(n=>n.dataset.messageId===id);
          const m=document.querySelector('#view');if(!e)return false;const r=e.getBoundingClientRect(),b=m.getBoundingClientRect();
          return r.top<b.bottom && r.bottom>b.top;}""", arg=rids[order[0]])
        assert sent_count(page, "ask") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reply_under_sheet_after_a_read_selection_is_recognized_when_uncovered(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('chat',{space:'agent'});ask('agent','Pregunta previa');goTab('home')")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='ask')[0]")
        deliver(page, first, {"text": "Respuesta previa", "actions": []})
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.messageId)")
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.messageId)")
        page.evaluate("""() => {ask('agent','Pregunta nueva');openSheet('Panel sintético',[h('p',{},'Cubierto')]);}""")
        second = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='ask')[0]")
        deliver(page, second, {"text": "Respuesta nueva cubierta", "actions": []})
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=second)
        page.evaluate("closeSheet()")
        page.wait_for_function("id=>document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=second, timeout=3000)
        page.get_by_text("Respuesta nueva cubierta", exact=False).first.wait_for(state="visible")
        assert sent_count(page, "ask") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_closing_sheet_without_new_reply_does_not_clear_a_different_result(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('results',{q:'primera',engine:'web',mode:'standard'});goTab('home')")
        first = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, first, {"engine": "web", "results": [{"title": "Resultado primero", "url": "https://example.test/1"}]})
        page.evaluate("go('results',{q:'segunda',engine:'web',mode:'standard'});goTab('home')")
        second = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, second, {"engine": "web", "results": [{"title": "Resultado segundo", "url": "https://example.test/2"}]})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===2")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text("Resultado segundo", exact=True).wait_for(state="visible")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.evaluate("openSheet('Panel sintético',[h('p',{},'Cubierto')]);closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden")
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.category==='web').target.params.q") == "primera"
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.get_by_text("Resultado primero", exact=True).wait_for(state="visible")
        assert sent_count(page, "search") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_two_same_destination_responses_under_sheet_show_the_selected_snapshot(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        rids = page.evaluate("""() => {window.__delay=60000;
          const args={q:'igual',engine:'web',mode:'standard'};go('results',args);
          const [first,meta]=Object.entries(bridge.pending()).find(([,m])=>m.op==='search');
          const second=bridge.request('search',args,meta.ctx).id;
          openSheet('Panel sintético',[h('p',{},'Cubierto')]);return [first,second];}""")
        deliver(page, rids[0], {"engine": "web", "results": [{"title": "Respuesta anterior cubierta", "url": "https://example.test/old"}]})
        page.wait_for_function("document.querySelector('#view').textContent.includes('Respuesta anterior cubierta')")
        deliver(page, rids[1], {"engine": "web", "results": [{"title": "Respuesta posterior cubierta", "url": "https://example.test/new"}]})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===2")
        page.evaluate("closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden && store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.get_by_text("Respuesta posterior cubierta", exact=True).wait_for(state="visible", timeout=2500)
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.get_by_text("Respuesta anterior cubierta", exact=True).wait_for(state="visible")
        assert sent_count(page, "search") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_gallery_result_waits_for_sheet_and_async_gallery(engine, failed):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;go('images');createImage('Imagen cubierta');
          openSheet('Panel sintético',[h('p',{},'Cubierto')]);}""")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='image')[0]")
        data = {"prompt": "Imagen cubierta", "source": "Fixture", "mime": "image/png",
                "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="}
        deliver(page, rid, data, "Fallo de imagen cubierto" if failed else "")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.imageId===id)", arg=rid)
        page.wait_for_function("imagesReadyToAcknowledge")
        assert page.evaluate("!document.querySelector('#sheet').hidden")
        page.evaluate("closeSheet()")
        page.wait_for_function("id=>document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.imageId===id)", arg=rid)
        if failed:
            page.locator(".image-failure").get_by_text("Fallo de imagen cubierto", exact=False).wait_for(state="visible")
        else:
            page.locator('.thumb img[alt="Imagen cubierta"]').wait_for(state="visible")
        assert sent_count(page, "image") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_expired_result_is_not_acknowledged_behind_sheet(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('results',{q:'caducada',engine:'web',mode:'standard'});goTab('home')")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, rid, {"engine": "web", "results": [{"title": "Se agotó la caché", "url": "https://example.test/old"}]})
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='caducada')")
        result = page.evaluate("""() => {const item=store.get('notices',[]).find(n=>n.unread&&n.target?.params?.q==='caducada');
          memo.delete(item.cacheKey);resultMemoTimes.delete(item.cacheKey);store.del(RESULT_CACHE_KEY);
          openSheet('Panel sintético',[h('p',{},'Cubierto')]);return openNotice(item);}""")
        # The destination can render its persistent expiry warning behind the
        # sheet, but it cannot be acknowledged while covered.
        assert result is True
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='caducada')")
        page.evaluate("closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden")
        page.get_by_text("El resultado guardado ya no está disponible", exact=False).first.wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='caducada')")
        assert sent_count(page, "search") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_opening_destination_then_covering_before_paint_keeps_it_unread(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('results',{q:'antes de pintar',engine:'web',mode:'standard'});goTab('home')")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        deliver(page, rid, {"engine": "web", "results": [{"title": "Respuesta sin pintar", "url": "https://example.test/unpainted"}]})
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='antes de pintar')")
        page.evaluate("goTab('web');openSheet('Panel sintético',[h('p',{},'Cubierto')])")
        assert page.evaluate("document.querySelector('#view').classList.contains('enter')")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='antes de pintar')")
        page.evaluate("closeSheet()")
        page.get_by_text("Respuesta sin pintar", exact=True).wait_for(state="visible")
        page.wait_for_function("document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='antes de pintar')")
        assert sent_count(page, "search") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reply_arriving_before_view_paints_is_not_lost_on_quick_navigation(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;go('results',{q:'llegada instantánea',engine:'web',mode:'standard'});
          const id=Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0];
          window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:true,
            data:{engine:'web',results:[{title:'Respuesta instantánea',url:'https://example.test/instant'}]}}});
          goTab('apps');}""")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='llegada instantánea')")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text("Respuesta instantánea", exact=True).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='llegada instantánea')")
        assert sent_count(page, "search") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_resuming_does_not_consume_an_unshown_reply_for_the_same_search(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        first = page.evaluate("""() => {window.__delay=60000;go('results',{q:'misma consulta',engine:'web',mode:'standard'});
          const id=Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0];goTab('home');return id;}""")
        context = page.evaluate("id=>bridge.pending()[id].ctx", first)
        deliver(page, first, {"engine": "web", "results": [{"title": "Primera sin mostrar", "url": "https://example.test/1"}]})
        second = page.evaluate("ctx=>bridge.request('search',{q:'misma consulta',engine:'web',mode:'standard'},ctx).id", context)
        deliver(page, second, {"engine": "web", "results": [{"title": "Segunda mostrada", "url": "https://example.test/2"}]})
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===2")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text("Segunda mostrada", exact=True).wait_for(state="visible")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
          document.dispatchEvent(new Event('visibilitychange'));
          Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        page.wait_for_timeout(350)
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length") == 1
        assert page.get_by_text("Segunda mostrada", exact=True).is_visible()
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.get_by_text("Primera sin mostrar", exact=True).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.category==='web')")
        assert sent_count(page, "search") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_message_not_acknowledged_if_sheet_covers_it_before_focus(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0 && !syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;
          store.set('chat.agent',Array.from({length:25},(_,i)=>({role:'user',text:'Historial sintético '+i,ts:now()-100+i})));
          go('chat',{space:'agent'});}""")
        page.wait_for_timeout(400)
        page.evaluate("""() => {ask('agent','Consulta nueva');
          Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='ask')[0]")
        deliver(page, rid, {"text": "Respuesta fuera del encuadre", "actions": []})
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=rid)
        page.wait_for_timeout(120)
        offscreen = page.evaluate("""id=>{const m=document.querySelector('#view');m.scrollTop=0;
          const e=Array.from(m.querySelectorAll('[data-message-id]')).find(n=>n.dataset.messageId===id);
          return e.getBoundingClientRect().top>=m.getBoundingClientRect().bottom;}""", rid)
        assert offscreen
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
          markVisibleNoticesRead();openSheet('Panel sintético',[h('p',{},'Cubierto')]);}""")
        assert page.evaluate("id=>store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", rid)
        page.evaluate("closeSheet()")
        page.wait_for_function("id=>document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=rid)
        page.wait_for_function("""id=>{const m=document.querySelector('#view');
          const e=Array.from(m.querySelectorAll('[data-message-id]')).find(n=>n.dataset.messageId===id);
          const r=e.getBoundingClientRect(),b=m.getBoundingClientRect();return r.top<b.bottom&&r.bottom>b.top;}""", arg=rid)
        assert sent_count(page, "ask") == 1
        assert errors == []
