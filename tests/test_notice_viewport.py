"""Inline results outside the viewport; all data and requests are synthetic."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


IMAGE = {
    "prompt": "Imagen fuera del encuadre",
    "source": "Fixture",
    "mime": "image/png",
    "b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
}


def deliver(page, rid, data=None, error=""):
    page.evaluate("""value=>window.__listener({serial:bridge.serial+1,payload:{t:'res',re:value.id,
      ok:!value.error,data:value.error?null:value.data,error:value.error}})""", {"id": rid, "data": data, "error": error})


def outside_view(page, selector):
    return page.evaluate("""selector=>{const m=document.querySelector('#view'),e=m.querySelector(selector);
      if(!e)return false;const r=e.getBoundingClientRect(),b=m.getBoundingClientRect();
      return r.top>=b.bottom||r.bottom<=b.top;}""", selector)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_download_outside_viewport_keeps_badge_until_seen(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {goTab('downloads');setJobs(Array.from({length:20},(_,i)=>({id:i+1,
          title:'Fichero sintético '+(i+1),status:'queued',kind:'file',created:now(),parts:0})));}""")
        page.wait_for_function("viewReady()")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        assert outside_view(page, '[data-job-id="1"]')
        page.evaluate("""bridge.pushes.job({id:1,title:'Fichero sintético 1',status:'failed',kind:'file',
          error:'Fallo fuera de pantalla',created:now(),parts:0})""")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        page.locator('.tab[data-tab="downloads"]:visible .tab-badge').wait_for(state="visible")
        assert page.locator('.tab[data-tab="downloads"]:visible .tab-badge').inner_text() == "1"
        page.locator('[data-job-id="1"]').scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        assert page.locator('.tab[data-tab="downloads"]:visible .tab-badge').is_hidden()
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_image_outside_viewport_keeps_badge_until_seen(engine, failed):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {window.__delay=60000;
          cache.models.images=Array.from({length:10},(_,i)=>({key:'test'+i+':model',label:'Modelo '+i,used:0,daily:10}));
          go('images');}""")
        page.wait_for_function("viewReady()&&imagesReadyToAcknowledge")
        # Results intentionally moved next to the prompt and no longer sit
        # below ten expanded model rows. Make this fixture genuinely offscreen
        # without reverting the compact UI or changing notification assertions.
        page.add_style_tag(content="#view .image-model-picker { margin-bottom: 1400px; }")
        rid = page.evaluate("""()=>{document.querySelector('#view').scrollTop=0;
          createImage('Imagen fuera del encuadre');
          return Object.entries(bridge.pending()).find(([,x])=>x.op==='image')[0];}""")
        deliver(page, rid, IMAGE, "Fallo fuera del encuadre" if failed else "")
        page.wait_for_function("id=>imagesReadyToAcknowledge && !!document.querySelector('[data-image-id=\"'+id+'\"]')", arg=rid)
        selector = f'[data-image-id="{rid}"]'
        assert outside_view(page, selector)
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.imageId===id)", arg=rid, timeout=3000)
        page.locator('.tab[data-tab="ai"]:visible .tab-badge').wait_for(state="visible")
        assert page.locator('.tab[data-tab="ai"]:visible .tab-badge').inner_text() == "1"
        page.locator(selector).scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.imageId===id)", arg=rid)
        assert page.locator('.tab[data-tab="ai"]:visible .tab-badge').is_hidden()
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='image').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_visible_download_does_not_create_unread_badge(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {goTab('downloads');setJobs([{id:1,title:'Fichero visible',status:'queued',
          kind:'file',created:now(),parts:0}]);}""")
        page.wait_for_function("viewReady()")
        assert not outside_view(page, '[data-job-id="1"]')
        page.evaluate("bridge.pushes.job({id:1,title:'Fichero visible',status:'done',kind:'file',created:now(),parts:0})")
        # The provisional durable record is not exposed as a badge while its
        # first frames are checked; once painted it becomes read.
        assert page.locator('.tab[data-tab="downloads"]:visible .tab-badge').is_hidden()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        assert page.locator('.tab[data-tab="downloads"]:visible .tab-badge').is_hidden()
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_visible_image_does_not_leave_a_badge(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('images')")
        page.wait_for_function("viewReady()&&imagesReadyToAcknowledge")
        rid = page.evaluate("""()=>{createImage('Imagen fuera del encuadre');
          return Object.entries(bridge.pending()).find(([,x])=>x.op==='image')[0];}""")
        deliver(page, rid, IMAGE)
        page.wait_for_function("id=>imagesReadyToAcknowledge && !!document.querySelector('[data-image-id=\"'+id+'\"]')", arg=rid)
        assert not outside_view(page, f'[data-image-id="{rid}"]')
        page.wait_for_function("id=>store.get('notices',[]).some(n=>!n.unread&&n.imageId===id)", arg=rid)
        assert page.locator('.tab[data-tab="ai"]:visible .tab-badge').is_hidden()
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='image').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_scrolling_one_download_does_not_clear_another_unshown(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {goTab('downloads');setJobs(Array.from({length:25},(_,i)=>({id:i+1,
          title:'Fichero sintético '+(i+1),status:'queued',kind:'file',created:now(),parts:0})));}""")
        page.wait_for_function("viewReady()")
        page.evaluate("""() => {document.querySelector('#view').scrollTop=0;
          for(const id of [1,12])bridge.pushes.job({id,title:'Fichero sintético '+id,status:'failed',kind:'file',
            error:'Fallo '+id,created:now(),parts:0});}""")
        assert outside_view(page, '[data-job-id="1"]')
        assert outside_view(page, '[data-job-id="12"]')
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='downloads').length") == 2
        page.wait_for_function("notificationCounts().downloads.unread===2")
        page.locator('[data-job-id="12"]').scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.job?.id==='12')")
        assert outside_view(page, '[data-job-id="1"]')
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        page.locator('.tab[data-tab="downloads"]:visible').click()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        assert not outside_view(page, '[data-job-id="1"]')
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_sheet_prevents_inline_download_from_being_read(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {goTab('downloads');setJobs(Array.from({length:20},(_,i)=>({id:i+1,
          title:'Fichero sintético '+(i+1),status:'queued',kind:'file',created:now(),parts:0})));}""")
        page.wait_for_function("viewReady()")
        page.evaluate("""() => {document.querySelector('#view').scrollTop=0;
          bridge.pushes.job({id:1,title:'Fichero sintético 1',status:'failed',kind:'file',error:'Fallo cubierto',created:now(),parts:0});
          openSheet('Panel sintético',[h('p',{},'Cubierto')]);
          document.querySelector('[data-job-id="1"]').scrollIntoView({block:'start'});}""")
        page.wait_for_timeout(150)
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        page.evaluate("closeSheet()")
        page.wait_for_function("document.querySelector('#sheet').hidden && !store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_utility_result_outside_viewport_is_not_silently_read(engine, failed):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('tools')")
        page.wait_for_function("viewReady()")
        for index in range(3):
            page.locator(".utility-card summary").nth(index).click()
        page.locator(".utility-card input").last.fill("1+1")
        page.locator(".utility-card button").last.click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='tool')[0]")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        deliver(page, rid, {"text": "Dos sintéticos"}, "Error sintético" if failed else "")
        output = page.locator(".utility-card").last.locator(".utility-body > div").last
        output.get_by_text("Error sintético" if failed else "Dos sintéticos", exact=False).first.wait_for(state="visible")
        assert outside_view(page, ".utility-card:last-of-type .utility-body > div:last-child")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=2500)
        page.locator('.tab[data-tab="apps"]:visible .tab-badge').wait_for(state="visible")
        assert page.locator('.tab[data-tab="apps"]:visible .tab-badge').inner_text() == "1"
        output.scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='tool').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_translation_outside_viewport_is_not_silently_read(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('translate')")
        page.wait_for_function("viewReady()")
        page.locator("#view textarea").fill("Texto de prueba. " * 60)
        page.locator(".context-help summary").click()
        page.get_by_role("button", name="Traducir", exact=True).click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='translate')[0]")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        deliver(page, rid, {"text": "Traducción sintética", "detected": "es"})
        output = page.locator(".translation")
        output.get_by_text("Traducción sintética", exact=False).wait_for(state="visible")
        assert outside_view(page, ".translation")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=2500)
        output.scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("how", ["scroll", "tab"])
def test_youtube_second_page_outside_viewport_keeps_its_own_badge(engine, how):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        first = page.evaluate("""()=>{window.__delay=60000;go('youtube',{q:'aves'});
          return Object.entries(bridge.pending()).find(([,x])=>x.op==='youtube_search')[0];}""")
        rows = [{"title": f"Vídeo sintético {index}", "url": f"https://youtube.com/watch?v={index}"} for index in range(15)]
        deliver(page, first, {"results": rows, "more": True})
        page.get_by_text("Vídeo sintético 14", exact=True).wait_for(state="visible")
        page.get_by_role("button", name="Mostrar más").click()
        second = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='youtube_search')[0]")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        deliver(page, second, {"results": [{"title": "Vídeo página dos", "url": "https://youtube.com/watch?v=second"}], "more": False})
        output = page.get_by_text("Vídeo página dos", exact=True)
        output.wait_for(state="visible")
        assert page.evaluate("""()=>{const r=Array.from(document.querySelectorAll('#view b')).find(n=>n.textContent==='Vídeo página dos').getBoundingClientRect();
          return r.top>=document.querySelector('#view').getBoundingClientRect().bottom;}""")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=second, timeout=2500)
        if how == "scroll":
            output.scroll_into_view_if_needed()
        else:
            page.locator('.tab[data-tab="apps"]:visible').click()
            page.get_by_text("Vídeo página dos", exact=True).wait_for(state="visible")
            assert page.evaluate("current.params.resultPage") == 2
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=second)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_chat_reply_does_not_pull_a_person_away_from_older_messages(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        rid = page.evaluate("""()=>{window.__delay=60000;
          store.set('chat.agent',Array.from({length:8},(_,i)=>({role:'assistant',rid:'old-'+i,
            text:'Conversación anterior '+i+' '.repeat(1)+'contenido anterior '.repeat(45),ts:now()})));
          go('chat',{space:'agent'});ask('agent','Nueva pregunta sintética');
          return Object.entries(bridge.pending()).find(([,x])=>x.op==='ask')[0];}""")
        page.wait_for_function("viewReady()")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        deliver(page, rid, {"text": "Respuesta fuera del encuadre", "actions": []})
        page.get_by_text("Respuesta fuera del encuadre", exact=False).wait_for(state="visible")
        page.wait_for_timeout(150)
        assert page.evaluate("document.querySelector('#view').scrollTop") < 50
        assert outside_view(page, f'[data-message-id="{rid}"]')
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=rid, timeout=2500)
        page.locator(f'[data-message-id="{rid}"]').scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='ask').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_two_unseen_translations_open_separately_without_repeating(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('translate')")
        page.wait_for_function("viewReady()")
        page.locator(".context-help summary").click()
        rids = []
        for index in (1, 2):
            page.locator("#view textarea").fill(("Texto de prueba %s. " % index) * 60)
            page.get_by_role("button", name="Traducir", exact=True).click()
            rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='translate')[0]")
            rids.append(rid)
            page.evaluate("document.querySelector('#view').scrollTop=0")
            deliver(page, rid, {"text": f"Traducción sintética {index}", "detected": "es"})
            page.get_by_text(f"Traducción sintética {index}", exact=True).wait_for(state="visible")
            assert outside_view(page, ".translation")
            page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='translate').length") == 2
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 2
        # Close/reopen: both snapshots must still be independently reachable.
        page.evaluate("store.set('tab','home')")
        page.reload()
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.wait_for_function("!syncing")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='translate').length") == 2
        page.locator('.tab[data-tab="apps"]:visible').click()
        page.get_by_text("Traducción sintética 2", exact=True).wait_for(state="visible")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='translate').length===1")
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.target?.view==='translate').resultId") == rids[0]
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.get_by_text("Traducción sintética 1", exact=True).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='translate')")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 0
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_visible_translation_and_web_reply_leave_no_badge(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('translate')")
        page.wait_for_function("viewReady()")
        page.locator("#view textarea").fill("Hola")
        page.get_by_role("button", name="Traducir", exact=True).click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='translate')[0]")
        deliver(page, rid, {"text": "Respuesta visible", "detected": "es"})
        page.get_by_text("Respuesta visible", exact=True).wait_for(state="visible")
        assert not outside_view(page, ".translation")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        page.locator('.tab[data-tab="apps"]:visible .tab-badge').wait_for(state="hidden")
        page.evaluate("go('results',{q:'visible',engine:'web',mode:'standard'})")
        page.wait_for_function("viewReady()")
        search = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='search')[0]")
        deliver(page, search, {"engine": "web", "results": [{"title": "Resultado visible", "url": "https://example.test/visible"}]})
        page.get_by_text("Resultado visible", exact=True).wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=search)
        page.locator('.tab[data-tab="web"]:visible .tab-badge').wait_for(state="hidden")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 1
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("feature", ["translation", "weather", "coder", "chat", "download"])
def test_leaving_before_inline_reply_paints_keeps_it_unread(engine, feature):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000")
        if feature == "translation":
            page.evaluate("go('translate')")
            page.wait_for_function("viewReady()")
            page.locator("#view textarea").fill("Hola")
            page.get_by_role("button", name="Traducir", exact=True).click()
            op, data, category, field = "translate", {"text": "Respuesta antes de salir", "detected": "es"}, "apps", "resultId"
        elif feature == "weather":
            page.evaluate("go('weather')")
            page.wait_for_function("viewReady()")
            page.locator("#view input").first.fill("Havana")
            page.get_by_role("button", name="Consultar").first.click()
            op, category, field = "weather", "apps", "resultId"
            data = {"query": "Havana", "place": {"id": 1, "label": "Havana"}, "candidates": [],
                    "current": {"temp": 28, "code": 0}, "days": [], "source": "Open-Meteo"}
        elif feature == "coder":
            page.evaluate("go('coder');coderSend('Cambio sintético')")
            page.wait_for_function("viewReady()")
            op, category, field = "code", "ai", "resultId"
            data = {"files": [], "project_complete": False, "summary": "Resumen sintético", "steps": []}
        elif feature == "chat":
            page.evaluate("go('chat',{space:'agent'});ask('agent','Pregunta sintética')")
            page.wait_for_function("viewReady()")
            op, category, field = "ask", "ai", "messageId"
            data = {"text": "Respuesta sintética", "actions": []}
        else:
            page.evaluate("goTab('downloads');setJobs([{id:1,title:'Archivo sintético',status:'queued',kind:'file',created:now(),parts:0}])")
            page.wait_for_function("viewReady()")
            op, category, field = "", "downloads", "job"
            data = {"id": 1, "title": "Archivo sintético", "status": "failed", "kind": "file", "error": "Error sintético", "parts": 0}
        rid = page.evaluate("op=>Object.entries(bridge.pending()).find(([,x])=>x.op===op)[0]", op) if op else "1"
        before_exit = page.evaluate("""v=>{if(v.op)window.__listener({serial:bridge.serial+1,payload:{t:'res',re:v.rid,ok:true,data:v.data}});
          else bridge.pushes.job(v.data);
          const item=store.get('notices',[]).find(n=>v.field==='job'?n.job?.id===v.rid:n[v.field]===v.rid);
          const visible=item&&elementInView(noticeNode(item));
          const badge=Array.from(document.querySelectorAll('.tab[data-tab="'+v.category+'"] .tab-badge')).some(n=>!n.hidden);
          goTab('home');return {visible,badge};}""",
                                    {"op": op, "rid": rid, "data": data, "field": field, "category": category})
        assert before_exit["visible"]
        assert not before_exit["badge"]
        query = "v=>store.get('notices',[]).some(n=>n.unread&&(v.field==='job'?n.job?.id===v.rid:n[v.field]===v.rid))"
        page.wait_for_function(query, arg={"rid": rid, "field": field}, timeout=3000)
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        page.wait_for_function("v=>!store.get('notices',[]).some(n=>n.unread&&(v.field==='job'?n.job?.id===v.rid:n[v.field]===v.rid))",
                               arg={"rid": rid, "field": field})
        if op:
            assert page.evaluate("op=>window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reopen_before_the_first_frame_recovers_the_unread_reply(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('translate')")
        page.wait_for_function("viewReady()")
        page.locator("#view textarea").fill("Hola")
        page.get_by_role("button", name="Traducir", exact=True).click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='translate')[0]")
        page.evaluate("""id=>{window.requestAnimationFrame=()=>0;
          window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:true,data:{text:'Respuesta guardada antes de pintar',detected:'es'}}});
          goTab('home');}""", rid)
        assert page.evaluate("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", rid)
        # Navigation exposes the saved badge even if the old view never runs
        # another animation callback (as can happen when a WebView is paused).
        assert not page.locator('.tab[data-tab="apps"]:visible .tab-badge').is_hidden()
        page.reload()
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.wait_for_function("!syncing")
        page.locator('.tab[data-tab="apps"]:visible .tab-badge').wait_for(state="visible")
        page.locator('.tab[data-tab="apps"]:visible').click()
        page.get_by_text("Respuesta guardada antes de pintar", exact=True).wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 0
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_backgrounding_before_paint_exposes_badge_without_an_animation_frame(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('translate')")
        page.wait_for_function("viewReady()")
        page.locator("#view textarea").fill("Hola")
        page.get_by_role("button", name="Traducir", exact=True).click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='translate')[0]")
        page.evaluate("""id=>{window.__originalRaf=window.requestAnimationFrame;window.requestAnimationFrame=()=>0;
          window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:true,data:{text:'En pausa',detected:'es'}}});
          Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});
          document.dispatchEvent(new Event('visibilitychange'));}""", rid)
        assert page.evaluate("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", rid)
        assert not page.locator('.tab[data-tab="apps"]:visible .tab-badge').is_hidden()
        page.evaluate("""()=>{window.requestAnimationFrame=window.__originalRaf;
          Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_scrolling_a_reply_into_view_then_leaving_before_paint_keeps_it_unread(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""() => {goTab('downloads');setJobs(Array.from({length:20},(_,i)=>({id:i+1,
          title:'Fichero sintético '+(i+1),status:'queued',kind:'file',created:now(),parts:0})));}""")
        page.wait_for_function("viewReady()")
        page.evaluate("""() => {document.querySelector('#view').scrollTop=0;
          bridge.pushes.job({id:1,title:'Fichero sintético 1',status:'failed',kind:'file',
            error:'Fallo sintético',created:now(),parts:0});}""")
        page.locator('.tab[data-tab="downloads"]:visible .tab-badge').wait_for(state="visible")
        assert outside_view(page, '[data-job-id="1"]')
        before_exit = page.evaluate("""()=>{const item=store.get('notices',[]).find(n=>n.job?.id==='1');
          noticeNode(item).scrollIntoView({block:'center'});
          const visible=elementInView(noticeNode(item));
          markInlineVisibleNoticesRead();goTab('home');return visible;}""")
        assert before_exit
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        page.locator('.tab[data-tab="downloads"]:visible').click()
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.job?.id==='1')")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_general_notice_visible_in_home_then_leaving_before_paint_stays_unread(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("goTab('home');notice('Aviso de referencia','info')")
        page.wait_for_function("viewReady()")
        page.locator('.notice').filter(has_text="Aviso de referencia").scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso de referencia').unread")
        before_exit = page.evaluate("""()=>{notice('Aviso fugaz','info');
          const item=store.get('notices',[]).find(n=>n.text==='Aviso fugaz');
          const visible=elementInView(noticeNode(item));goTab('ai');return visible;}""")
        assert before_exit
        assert page.evaluate("store.get('notices',[]).find(n=>n.text==='Aviso fugaz').unread")
        page.locator('.tab[data-tab="home"]:visible .tab-badge').wait_for(state="visible")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.locator('.notice').filter(has_text="Aviso fugaz").scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso fugaz').unread")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_expired_reply_is_not_read_if_warning_cannot_paint(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('results',{q:'caducada',engine:'web',mode:'standard'});goTab('apps')")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='search')[0]")
        deliver(page, rid, {"engine": "web", "results": [{"title": "Resultado sintético", "url": "https://example.test"}]})
        page.evaluate("""id=>{const n=store.get('notices',[]).find(x=>x.resultId===id);
          memo.delete(n.cacheKey);resultMemoTimes.delete(n.cacheKey);resultMemoErrors.delete(n.cacheKey);store.del(RESULT_CACHE_KEY);
          openNotice(n);goTab('home');}""", rid)
        assert page.evaluate("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", rid)
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text("El resultado guardado ya no está disponible", exact=False).first.wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("feature", ["web", "youtube"])
def test_expired_reply_does_not_attach_to_newer_pending_request_with_same_key(engine, feature):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        if feature == "web":
            page.evaluate("window.__delay=60000;go('results',{q:'misma',engine:'web',mode:'standard'});goTab('home')")
            op, category, args = "search", "web", {"q": "misma", "engine": "web", "mode": "standard"}
            data = {"engine": "web", "results": [{"title": "Respuesta nueva", "url": "https://example.test/new"}]}
        else:
            page.evaluate("window.__delay=60000;go('youtube',{q:'misma'});goTab('home')")
            op, category, args = "youtube_search", "apps", {"q": "misma", "page": 1}
            data = {"results": [{"title": "Respuesta nueva", "url": "https://youtube.com/watch?v=synthetic"}], "more": False}
        old = page.evaluate("op=>Object.entries(bridge.pending()).find(([,x])=>x.op===op)[0]", op)
        deliver(page, old, data)
        newer = page.evaluate("""v=>{const n=store.get('notices',[]).find(x=>x.resultId===v.old);
          memo.delete(n.cacheKey);resultMemoTimes.delete(n.cacheKey);resultMemoErrors.delete(n.cacheKey);store.del(RESULT_CACHE_KEY);
          return bridge.request(v.op,v.args,{kind:'view-result',key:n.cacheKey,target:n.target,
            category:v.category,label:'notification_search'}).id;}""", {"old": old, "op": op, "args": args, "category": category})
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        page.get_by_text("El resultado guardado ya no está disponible", exact=False).first.wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=old)
        assert page.evaluate("id=>!!bridge.pending()[id]", newer)
        assert page.evaluate("op=>window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 2
        deliver(page, newer, data)
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=newer)
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        page.get_by_text("Respuesta nueva", exact=True).first.wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=newer)
        assert page.evaluate("op=>window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_weather_outside_viewport_is_not_silently_read(engine, failed):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""()=>{window.__delay=60000;
          store.set('cities',Array.from({length:6},(_,i)=>('Municipio muy largo de la región ').repeat(4)+i));go('weather');}""")
        page.wait_for_function("viewReady()")
        page.locator("#view input").first.fill("Havana")
        page.get_by_role("button", name="Consultar").first.click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='weather')[0]")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        data = {"query": "Havana", "place": {"id": 1, "label": "Havana, Cuba"},
                "candidates": [{"id": 1, "label": "Havana, Cuba"}], "timezone": "America/Havana", "as_of": "2026-10-29",
                "current": {"temp": 28, "feels": 30, "humidity": 72, "wind": 13, "precipitation": 0,
                            "code": 0, "is_day": 1, "description": "clear"}, "days": [], "source": "Open-Meteo"}
        deliver(page, rid, data, "Fallo climático sintético" if failed else "")
        output = page.locator(".weather-result")
        output.get_by_text("Fallo climático sintético" if failed else "Havana, Cuba", exact=False).first.wait_for(state="visible")
        assert outside_view(page, ".weather-result")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=2500)
        output.scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='weather').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", [False, True])
def test_coder_result_outside_viewport_is_not_silently_read(engine, failed):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""()=>{window.__delay=60000;const c=coder.get();
          c.turns=Array.from({length:5},(_,i)=>({role:'assistant',text:'Anterior '+i+' '+('contenido anterior ').repeat(60),ts:now()}));
          coder.set(c);go('coder');coderSend('Cambio sintético');}""")
        page.wait_for_function("viewReady()")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='code')[0]")
        page.evaluate("document.querySelector('#view').scrollTop=0")
        data = {"files": [], "project_complete": False, "model_confirmed": True,
                "summary": "Resumen fuera de pantalla", "steps": [], "name": "Fixture"}
        deliver(page, rid, data, "Fallo del Programador" if failed else "")
        output = page.locator("#view .bubble").last
        output.get_by_text("Fallo del Programador" if failed else "Resumen fuera de pantalla", exact=False).first.wait_for(state="visible")
        assert output.evaluate("n=>n.getBoundingClientRect().top>=document.querySelector('#view').getBoundingClientRect().bottom")
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=2500)
        output.scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='code').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_concurrent_weather_replies_remain_individual_and_do_not_replace_latest(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('weather')")
        page.wait_for_function("viewReady()")
        for city in ("Havana", "Santiago"):
            page.locator("#view input").first.fill(city)
            page.get_by_role("button", name="Consultar").first.click()
        rids = page.evaluate("Object.entries(bridge.pending()).filter(([,x])=>x.op==='weather').map(([id])=>id)")
        assert len(rids) == 2
        page.locator('.tab[data-tab="home"]:visible').click()
        def forecast(city, place_id):
            return {"query": city, "place": {"id": place_id, "label": city},
                    "candidates": [{"id": place_id, "label": city}], "timezone": "America/Havana", "as_of": "2026-10-29",
                    "current": {"temp": 28, "feels": 30, "humidity": 72, "wind": 13, "precipitation": 0,
                                "code": 0, "is_day": 1, "description": "clear"}, "days": [], "source": "Open-Meteo"}
        deliver(page, rids[1], forecast("Santiago", 2))
        deliver(page, rids[0], forecast("Havana", 1))
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='weather').length===2")
        assert page.evaluate("weatherHome().last.data.query") == "Santiago"
        page.locator('.tab[data-tab="apps"]:visible').click()
        page.locator(".weather-place").get_by_text("Havana", exact=True).wait_for(state="visible")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='weather').length===1")
        assert page.evaluate("store.get('notices',[]).find(n=>n.unread&&n.target?.view==='weather').resultId") == rids[1]
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.locator(".weather-place").get_by_text("Santiago", exact=True).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='weather')")
        assert page.evaluate("weatherHome().last.data.query") == "Santiago"
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='weather').length") == 2
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("missing", [False, True])
def test_old_coder_notice_opens_its_saved_turn_or_explicit_expiry(engine, missing):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""missing=>{const p=coder.get();p.turns=Array.from({length:10},(_,i)=>({role:'assistant',
          rid:'turn-'+i,text:'Respuesta previa '+i,ts:now()}));coder.set(p);
          notice('Programador listo','ok',{category:'ai',target:{view:'coder',params:{}},resultId:missing?'missing-turn':'turn-0'});}""", missing)
        page.locator('.tab[data-tab="ai"]:visible').click()
        if missing:
            page.get_by_text("El resultado guardado ya no está disponible", exact=False).first.wait_for(state="visible")
        else:
            page.get_by_text("Respuesta previa 0", exact=True).wait_for(state="visible")
            page.get_by_text("los archivos corresponden al proyecto actual", exact=False).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='coder')")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='code').length") == 0
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_opening_a_closed_utility_marks_its_now_visible_result(engine):
    with browser_fixture("es", {"width": 1280, "height": 800}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('tools')")
        page.wait_for_function("viewReady()")
        details = page.locator(".utility-card").first
        details.locator("summary").click()
        details.locator("input").fill("1 EUR en CUP")
        details.locator("button").first.click()
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='tool')[0]")
        details.locator("summary").click()
        deliver(page, rid, {"text": "Resultado en sección cerrada"})
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert not details.get_by_text("Resultado en sección cerrada", exact=False).first.is_visible()
        details.locator("summary").click()
        result = details.get_by_text("Resultado en sección cerrada", exact=False).first
        result.wait_for(state="visible")
        # Expanded is not the same as in the viewport when local tools are
        # above this utility. Only actual exposure may acknowledge the result.
        result.scroll_into_view_if_needed()
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=3000)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='tool').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_chat_message_hidden_by_sticky_header_is_not_read(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""()=>{store.set('chat.agent',Array.from({length:9},(_,i)=>({role:'assistant',rid:'old-'+i,
          text:i===4?'Breve':('Conversación anterior ').repeat(80),ts:now()})));go('chat',{space:'agent'});}""")
        page.wait_for_function("viewReady()")
        page.evaluate("notice('Respuesta sintética','ok',{category:'ai',target:{view:'chat',params:{space:'agent'}},messageId:'old-4'})")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.messageId==='old-4')")
        # Wait for provisional badge exposure so its button no longer shifts
        # the chat geometry while we position the bubble behind the header.
        page.locator('.tab[data-tab="ai"]:visible .tab-badge').wait_for(state="visible")
        page.evaluate("""()=>{const m=document.querySelector('#view'),n=m.querySelector('[data-message-id="old-4"]');
          m.scrollTop+=n.getBoundingClientRect().top-m.getBoundingClientRect().top-1;}""")
        page.wait_for_timeout(150)
        assert page.evaluate("""()=>{const r=document.querySelector('[data-message-id="old-4"]').getBoundingClientRect(),
          h=document.querySelector('.agent-mode').getBoundingClientRect();return r.bottom>h.top&&r.bottom<=h.bottom+1;}""")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.messageId==='old-4')")
        page.evaluate("document.querySelector('#view').scrollTop-=document.querySelector('.agent-mode').getBoundingClientRect().height+12")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.messageId==='old-4')")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_slow_view_fade_eventually_acknowledges_without_early_read(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("window.__delay=60000;go('results',{q:'transición lenta',engine:'web',mode:'standard'});goTab('home')")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,x])=>x.op==='search')[0]")
        deliver(page, rid, {"engine": "web", "results": [{"title": "Respuesta de transición", "url": "https://example.test/fade"}]})
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        page.wait_for_function("viewReady()")
        page.add_style_tag(content=".view.enter { transition: none !important; } .view:not(.enter) { transition: opacity 1.2s linear !important; }")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.wait_for_timeout(350)
        assert page.evaluate("Number(getComputedStyle(document.querySelector('#view')).opacity)") < 0.5
        assert page.evaluate("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", rid)
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=5000)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert errors == []
