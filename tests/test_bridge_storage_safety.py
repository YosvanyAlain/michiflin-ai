"""Synthetic failures of local request tracking; no account or network is used."""
from __future__ import annotations

import pytest

from tests.test_notice_viewport import deliver
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_request_is_not_sent_when_minimum_tracking_cannot_be_saved(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        result = page.evaluate("""()=>{window.__delay=60000;const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return original.call(this,key,value);};
          const before=window.__sentUpdates.filter(x=>x.payload?.op==='ask').length;
          const accepted=ask('agent','Pregunta sintética');
          return {accepted,sends:window.__sentUpdates.filter(x=>x.payload?.op==='ask').length-before};}""")
        assert result == {"accepted": False, "sends": 0}
        assert page.get_by_text("la solicitud no se envió", exact=False).count() >= 1
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread)")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pending_request_falls_back_to_metadata_without_resend_copy(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        result = page.evaluate("""()=>{window.__delay=60000;const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending'&&Object.values(JSON.parse(value)).some(x=>x.p))
            throw new DOMException('full','QuotaExceededError');return original.call(this,key,value);};
          const accepted=ask('agent','Pregunta sintética');
          const item=Object.entries(bridge.pending()).find(([,x])=>x.op==='ask');
          return {accepted,id:item?.[0]||'',copy:!!item?.[1].p};}""")
        assert result["accepted"] and result["id"] and not result["copy"]
        assert page.get_by_text("faltó espacio para copias de reenvío", exact=False).count() >= 1
        rid = result["id"]
        page.evaluate("""id=>{const p=bridge.pending();p[id].sent=Date.now()-60000;p[id].ts=Date.now()-60000;
          bridge.save(p);bridge.tick(true);}""", rid)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='ask').length") == 1
        deliver(page, rid, {"text": "Respuesta conservada", "actions": []})
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.messageId===id)", arg=rid)
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_resend_is_not_sent_if_updated_retry_state_cannot_be_saved(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        result = page.evaluate("""()=>{window.__delay=60000;const id=bridge.request('search',{q:'sintética'}).id;
          const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return original.call(this,key,value);};
          const refused=bridge.resend(id);const after=window.__sentUpdates.filter(x=>x.payload?.op==='search').length;
          Storage.prototype.setItem=original;const accepted=bridge.resend(id);
          const messages=window.__sentUpdates.filter(x=>x.payload?.op==='search').map(x=>x.payload);
          return {id,refused,after,accepted,messages};}""")
        assert result["refused"] is False and result["after"] == 1
        assert result["accepted"] is True and len(result["messages"]) == 2
        assert result["messages"][1]["id"] == result["id"] and result["messages"][1]["retry"] == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_retry_button_never_claims_a_failed_resend_was_sent(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""()=>{window.__delay=60000;bridge.request('search',{q:'sintética'});pendingSheet();
          const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return original.call(this,key,value);};}""")
        page.get_by_role("button", name="Reintentar", exact=True).click()
        page.get_by_text("no se reenvió la solicitud", exact=False).wait_for(state="visible")
        assert page.get_by_text("Solicitud reenviada.", exact=True).count() == 0
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_resend_uses_same_payload_when_optional_copy_is_dropped_to_fit(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        result = page.evaluate("""()=>{window.__delay=60000;const id=bridge.request('search',{q:'sintética'}).id;
          const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending'&&Object.values(JSON.parse(value)).some(x=>x.p))
            throw new DOMException('full','QuotaExceededError');return original.call(this,key,value);};
          const first=bridge.resend(id), second=bridge.resend(id);
          const messages=window.__sentUpdates.filter(x=>x.payload?.op==='search').map(x=>x.payload);
          return {id,first,second,messages,hasCopy:!!bridge.pending()[id]?.p};}""")
        assert result["first"] and not result["second"] and not result["hasCopy"]
        assert len(result["messages"]) == 2
        assert result["messages"][1]["id"] == result["id"]
        assert result["messages"][1]["args"] == {"q": "sintética"}
        assert result["messages"][1]["retry"] == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_large_code_request_never_sends_partial_updates_when_tracking_fails(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        result = page.evaluate("""()=>{window.__delay=60000;const original=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return original.call(this,key,value);};
          const before=window.__sentUpdates.length;
          const req=bridge.request('code',{text:'Cambio sintético',files:[{path:'ejemplo.txt',text:'x'.repeat(150000)}]});
          return {id:req.id,sends:window.__sentUpdates.length-before};}""")
        assert result == {"id": None, "sends": 0}
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_failed_settlement_write_does_not_retry_or_deliver_twice_in_open_session(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        rid = page.evaluate("""()=>{window.__delay=60000;ask('agent','Pregunta sintética');
          const id=Object.entries(bridge.pending()).find(([,x])=>x.op==='ask')[0];
          window.__storageSet=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return window.__storageSet.call(this,key,value);};return id;}""")
        deliver(page, rid, {"text": "Una respuesta", "actions": []})
        assert not page.evaluate("id=>!!bridge.pending()[id]", rid)
        assert not page.evaluate("id=>bridge.resend(id)", rid)
        deliver(page, rid, {"text": "Duplicado ignorado", "actions": []})
        assert page.evaluate("id=>store.get('notices',[]).filter(n=>n.messageId===id).length", rid) == 1
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='ask').length") == 1
        page.evaluate("Storage.prototype.setItem=window.__storageSet;bridge.save(bridge.pending())")
        assert not page.evaluate("id=>!!store.get('pending',{})[id]", rid)
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("outcome", ["settled", "cancelled"])
def test_failed_cleanup_never_revives_an_acknowledged_request_in_the_same_opening(engine, outcome):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        rid = page.evaluate("""()=>{window.__delay=60000;ask('agent','Pregunta sintética');
          const id=Object.entries(bridge.pending()).find(([,x])=>x.op==='ask')[0];
          bridge.acked(id);window.__storageSet=Storage.prototype.setItem;
          Storage.prototype.setItem=function(key,value){if(key==='mf.pending')throw new DOMException('full','QuotaExceededError');
            return window.__storageSet.call(this,key,value);};return id;}""")
        if outcome == "settled":
            deliver(page, rid, {"text": "Respuesta original", "actions": []})
        else:
            page.evaluate("id=>bridge.forget(id)", rid)
        assert page.evaluate("id=>!!store.get('pending',{})[id]?.ack", rid)  # failed cleanup left the old record
        assert not page.evaluate("id=>!!bridge.pending()[id]", rid)
        # The acknowledged record is not subject to the unacknowledged stale
        # limit. Advancing the clock must never resurrect it in this opening.
        # Also model a temporary read error before storage becomes available.
        page.evaluate("""()=>{window.__realNow=Date.now;Date.now=()=>window.__realNow()+STALE_MS+1000;
          window.__storageGet=Storage.prototype.getItem;Storage.prototype.getItem=function(key){
            if(key==='mf.pending')throw new Error('temporarily unavailable');return window.__storageGet.call(this,key);};
          bridge.pending();Storage.prototype.getItem=window.__storageGet;}""")
        assert not page.evaluate("id=>!!bridge.pending()[id]", rid)
        page.evaluate("()=>{Storage.prototype.setItem=window.__storageSet;}")
        assert not page.evaluate("id=>bridge.resend(id)", rid)
        deliver(page, rid, {"text": "Duplicado ignorado", "actions": []})
        assert page.evaluate("id=>store.get('notices',[]).filter(n=>n.messageId===id).length", rid) == (1 if outcome == "settled" else 0)
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='ask').length") == 1
        page.evaluate("()=>{Date.now=window.__realNow;}")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("feature", ["translate", "weather"])
@pytest.mark.parametrize("reopen", [False, True])
def test_reply_is_reachable_when_latest_marker_cannot_be_saved(engine, feature, reopen):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        page.evaluate("""feature=>{window.__delay=60000;const original=Storage.prototype.setItem;
          const blocked=feature==='translate'?'mf.tr.latest':'mf.weather.latest';
          Storage.prototype.setItem=function(key,value){if(key===blocked)throw new DOMException('full','QuotaExceededError');
            return original.call(this,key,value);};go(feature);}""", feature)
        page.wait_for_function("viewReady()")
        if feature == "translate":
            page.locator("#view textarea").fill("Hola")
            page.get_by_role("button", name="Traducir", exact=True).click()
            data = {"text": "Resultado sintético", "detected": "es"}
        else:
            page.locator("#view input").first.fill("Havana")
            page.get_by_role("button", name="Consultar").first.click()
            data = {"query": "Havana", "place": {"id": 1, "label": "Havana"}, "candidates": [],
                    "current": {"temp": 28, "code": 0}, "days": [], "source": "Open-Meteo"}
        rid = page.evaluate("op=>Object.entries(bridge.pending()).find(([,x])=>x.op===op)[0]", feature)
        page.evaluate("goTab('home')")
        if reopen:
            page.reload()
            page.locator("#splash").wait_for(state="detached", timeout=8000)
            page.wait_for_function("window.__listener&&cache.at>0&&!syncing")
            assert page.evaluate("id=>!!bridge.pending()[id]", rid)
        deliver(page, rid, data)
        page.wait_for_function("id=>store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid, timeout=3000)
        page.locator('.tab[data-tab="apps"]:visible').click()
        page.get_by_text("Resultado sintético" if feature == "translate" else "Havana", exact=False).first.wait_for(state="visible")
        page.wait_for_function("id=>!store.get('notices',[]).some(n=>n.unread&&n.resultId===id)", arg=rid)
        assert page.evaluate("op=>window.__sentUpdates.filter(x=>x.payload?.op===op).length", feature) == (0 if reopen else 1)
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("old_error", [False, True])
def test_older_concurrent_translation_remains_reachable_without_replacing_latest(engine, old_error):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing&&Object.keys(bridge.pending()).length===0")
        page.locator("#splash").wait_for(state="detached", timeout=8000)
        rids = page.evaluate("""()=>{window.__delay=60000;
          const ctx={kind:'translate-result',category:'apps',target:{view:'translate',params:{}}};
          const first=bridge.request('translate',{text:'Uno',source:'es',target:'en'},ctx).id;
          const latest=bridge.request('translate',{text:'Dos',source:'es',target:'en'},ctx).id;
          latestTranslationId=latest;store.set('tr.latest',latest);return [first,latest];}""")
        deliver(page, rids[1], {"text": "Traducción reciente", "detected": "es"})
        deliver(page, rids[0], {"text": "Traducción anterior", "detected": "es"}, "Fallo anterior" if old_error else "")
        assert page.evaluate("id=>store.get('tr.result',{}).rid===id", rids[1])
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='translate').length") == 2
        page.locator('.tab[data-tab="apps"]:visible').click()
        page.get_by_text("Fallo anterior" if old_error else "Traducción anterior", exact=False).first.wait_for(state="visible")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.target?.view==='translate').length===1")
        page.get_by_role("button", name="Ver respuestas pendientes (1)").click()
        page.get_by_text("Traducción reciente", exact=True).wait_for(state="visible")
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='translate')")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='translate').length") == 2
        assert errors == []
