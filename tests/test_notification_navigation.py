"""Notification destinations with a synthetic bridge; never contacts a live bot."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


def sent_count(page, op):
    return page.evaluate("op => window.__sentUpdates.filter(x => x.payload?.op === op).length", op)


@pytest.mark.parametrize("engine,viewport,touch", [
    ("chromium", {"width": 1280, "height": 800}, False),
    ("webkit", {"width": 390, "height": 844}, True),
])
def test_web_tab_returns_to_completed_result_without_repeat(engine, viewport, touch):
    with browser_fixture("es", viewport, engine=engine, touch=touch) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => {
          // Hold only this synthetic response until the test explicitly releases it.
          // A fixed delay can expire while a slower runner is changing tabs.
          const original = window.webxdc.sendUpdate;
          window.webxdc.sendUpdate = (update, ...args) => {
            if (update?.payload?.t === 'req' && update.payload.op === 'search') {
              window.__sentUpdates.push(update);
              window.__heldSearch = update.payload;
              return;
            }
            return original.call(window.webxdc, update, ...args);
          };
        }""")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.locator('#view input[type="search"]').fill('consulta sintética')
        page.get_by_role('button', name='Ir', exact=True).click()
        page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'search')")
        page.locator('.tab[data-tab="apps"]:visible').click()
        web_tab = page.locator('.tab[data-tab="web"]:visible')
        assert page.evaluate("!!window.__heldSearch && !!bridge.pending()[window.__heldSearch.id]")
        assert not page.evaluate("window.__fixtureResponses.includes('search')")
        assert web_tab.locator('.tab-badge').is_hidden()  # activity alone never adds an unread badge
        page.evaluate("""() => {
          const request = window.__heldSearch;
          window.__fixtureResponses.push('search');
          window.__listener({serial: ++window.__serial, payload: {t:'res', re:request.id, ok:true,
            data:{engine:'web',results:[{title:'Resultado sintético de navegación',
              url:'https://example.test/result',snippet:'Prueba'}]}}});
        }""")
        page.wait_for_function("store.get('notices',[]).some(n => n.unread && n.target?.view === 'results')")
        assert '1 sin leer' in web_tab.get_attribute('aria-label')
        web_tab.click()
        assert page.evaluate('current.view') == 'results'
        page.get_by_text('Resultado sintético de navegación', exact=True).wait_for(state='visible')
        web_tab.locator('.tab-badge').wait_for(state='hidden')
        assert sent_count(page, 'search') == 1
        assert errors == []


def test_second_youtube_page_notice_opens_actual_page_without_repeat():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => { window.__delay=650; window.__searchResults=Array.from({length:20},(_,i)=>({
          title:'Vídeo sintético '+(i+1),url:'https://youtube.com/watch?v=sample'+(i+1),snippet:'Prueba'}));
          go('youtube',{q:'páginas sintéticas'}); }""")
        page.get_by_text('Vídeo sintético 15', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Mostrar más resultados').click()
        page.wait_for_function("window.__sentUpdates.filter(x => x.payload?.op==='youtube_search').length === 2")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n => n.unread && n.target?.view === 'youtube')")
        page.locator('.tab[data-tab="apps"]:visible').click()
        assert page.evaluate('current.view') == 'youtube'
        page.get_by_text('Vídeo sintético 16', exact=True).wait_for(state='visible')
        assert sent_count(page, 'youtube_search') == 2
        page.wait_for_function("!store.get('notices',[]).some(n => n.unread && n.target?.view === 'youtube')")
        assert errors == []


def test_image_failure_is_visible_in_destination_and_not_repeated():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=700; window.__failNext.image=1; go('images')")
        page.locator('#view textarea').first.fill('Imagen sintética')
        page.get_by_role('button', name='Generar', exact=True).click()
        page.wait_for_function("window.__sentUpdates.some(x=>x.payload?.op==='image')")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread && n.kind==='bad' && n.target?.view==='images')")
        page.locator('.tab[data-tab="ai"]:visible').click()
        assert page.evaluate('current.view') == 'images'
        page.get_by_text('SIMULATED network uncertainty', exact=False).first.wait_for(state='visible')
        assert sent_count(page, 'image') == 1
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread && n.target?.view==='images')")
        assert errors == []


def test_multiple_web_notices_are_opened_one_at_a_time():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=550;window.__searchResults=[{title:'Respuesta uno',url:'https://example.test/1'}];
          go('results',{q:'primera',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.evaluate("""() => {window.__searchResults=[{title:'Respuesta dos',url:'https://example.test/2'}];
          go('results',{q:'segunda',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===2")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Respuesta dos', exact=True).wait_for(state='visible')
        assert page.evaluate('current.params.q') == 'segunda'
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        assert '1 sin leer' in page.locator('.tab[data-tab="web"]:visible').get_attribute('aria-label')
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Respuesta uno', exact=True).wait_for(state='visible')
        assert page.evaluate('current.params.q') == 'primera'
        page.locator('.tab[data-tab="web"]:visible .tab-badge').wait_for(state='hidden')
        assert sent_count(page, 'search') == 2
        assert errors == []


def test_expired_web_notice_warns_and_keeps_other_unread_results():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Conservada',url:'https://example.test/1'}];
          go('results',{q:'primera',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.evaluate("""() => {window.__searchResults=[{title:'Caducada',url:'https://example.test/2'}];
          go('results',{q:'segunda',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===2")
        page.evaluate("""() => { const item=store.get('notices',[]).find(n=>n.unread&&n.target?.params?.q==='segunda');
          resultMemoTimes.set(item.cacheKey,now()-RESULT_CACHE_SECONDS-1);
          const saved=store.get(RESULT_CACHE_KEY,{});saved[item.cacheKey].at=now()-RESULT_CACHE_SECONDS-1;store.set(RESULT_CACHE_KEY,saved); }""")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread&&n.category==='web').length===1")
        page.get_by_role('button', name='Ver respuestas pendientes (1)').click()
        page.get_by_text('Conservada', exact=True).wait_for(state='visible')
        page.locator('.tab[data-tab="web"]:visible .tab-badge').wait_for(state='hidden')
        assert sent_count(page, 'search') == 2
        assert errors == []


def test_response_after_reopen_restores_from_persisted_request_without_resending():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=60000;go('results',{q:'reapertura',engine:'web',mode:'standard'});goTab('apps');}""")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='search')[0]")
        page.reload()
        page.wait_for_function("window.__listener && cache.at > 0")
        assert page.evaluate("id => !!bridge.pending()[id]", rid)
        assert sent_count(page, 'search') == 0
        page.evaluate("""id => {window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:true,
          data:{engine:'web',results:[{title:'Respuesta tras reapertura',url:'https://example.test/reopen'}]}}});}""", rid)
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        # The boot splash is also present on reload; wait until the view is usable.
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Respuesta tras reapertura', exact=True).wait_for(state='visible')
        assert sent_count(page, 'search') == 0
        page.locator('.tab[data-tab="web"]:visible .tab-badge').wait_for(state='hidden')
        assert errors == []


@pytest.mark.parametrize('feature,fail', [
    ('translate', False), ('translate', True), ('weather', False), ('weather', True),
    ('tools', False), ('tools', True), ('news', False), ('news', True),
    ('music', False), ('music', True), ('chat', False), ('chat', True),
    ('coder', False), ('coder', True),
])
def test_section_tabs_open_other_notified_outcomes(feature, fail):
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('window.__delay=700')
        op = {'translate':'translate','weather':'weather','tools':'tool','news':'search',
              'music':'music_search','chat':'ask','coder':'code'}[feature]
        if fail:
            page.evaluate('op => window.__failNext[op]=1', op)
        if feature == 'chat':
            page.evaluate("go('chat',{space:'agent'});ask('agent','Pregunta sintética')")
        elif feature == 'coder':
            page.evaluate("go('coder');coderSend('Proyecto sintético')")
        elif feature == 'news':
            page.evaluate("window.__searchResults=[{title:'Noticia sintética',url:'https://example.test/news'}];go('news',{q:'prueba'})")
        elif feature == 'music':
            page.evaluate("go('music',{q:'jazz',provider:'archive'})")
        else:
            page.evaluate('name => go(name)', feature)
            if feature == 'translate':
                page.locator('#view textarea').fill('Hola')
                page.get_by_role('button', name='Traducir', exact=True).click()
            elif feature == 'weather':
                page.locator('#view input').first.fill('Havana')
                page.get_by_role('button', name='Consultar').first.click()
            else:
                page.locator('.utility-tile').filter(has_text='Conversor de unidades').click()
                page.locator('.utility-workspace input').first.fill('1 EUR en CUP')
                page.locator('.utility-workspace .search-field button').first.click()
        page.wait_for_function('op => window.__sentUpdates.some(x=>x.payload?.op===op)', arg=op)
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function('name => store.get("notices",[]).some(n=>n.unread&&n.target?.view===name)', arg=feature)
        category = 'ai' if feature in ('chat','coder') else 'apps'
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        assert page.evaluate('current.view') == feature
        expected = 'SIMULATED network uncertainty' if fail else {
            'translate':'translated smoke test','weather':'Havana, Cuba','tools':'smoke test result',
            'news':'Noticia sintética','music':'Licensed jazz','chat':'SIMULATED agent answer','coder':'Proyecto sintético listo',
        }[feature]
        result = page.locator('.weather-place').get_by_text(expected, exact=False) if feature == 'weather' and not fail else page.get_by_text(expected, exact=False).first
        result.wait_for(state='visible')
        page.wait_for_function('name => !store.get("notices",[]).some(n=>n.unread&&n.target?.view===name)', arg=feature)
        assert sent_count(page, op) == 1
        assert errors == []


def test_image_success_gallery_and_failure_survive_reopen():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=500;go('images');createImage('Imagen que funciona');goTab('home')")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='images')")
        page.reload()
        page.wait_for_function('window.__listener && cache.at > 0')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.locator('.gallery img[alt="Imagen que funciona"]').wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='images')")
        assert sent_count(page, 'image') == 0
        page.evaluate("window.__delay=500;window.__failNext.image=1;createImage('Imagen que falla');goTab('home')")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.kind==='bad'&&n.target?.view==='images')")
        page.reload()
        page.wait_for_function('window.__listener && cache.at > 0')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.locator('.image-failure').get_by_text('SIMULATED network uncertainty', exact=False).wait_for(state='visible')
        page.get_by_text('Imagen que falla', exact=True).wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='images')")
        assert sent_count(page, 'image') == 0
        assert errors == []


def test_missing_image_and_chat_results_are_explained_not_reissued():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("window.__delay=500;go('images');createImage('Imagen perdida');goTab('home')")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='images')")
        page.evaluate('media.clear()')
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        assert sent_count(page, 'image') == 1
        page.evaluate("window.__delay=500;go('chat',{space:'agent'});ask('agent','Pregunta perdida');goTab('home')")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='chat')")
        page.evaluate("store.set('chat.agent',[])")
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.locator('#view').get_by_text('El resultado guardado ya no está disponible', exact=False).wait_for(state='visible')
        assert sent_count(page, 'ask') == 1
        assert errors == []


def test_download_notice_shows_evicted_failure_details_without_retry():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {bridge.pushes.job({id:7,title:'Archivo sintético',status:'failed',kind:'file',
          error:'Fallo sintético de descarga',warning:'Sin volver a intentar',created:now(),parts:0});
          cache.jobs=[];saveCache();}""")
        page.locator('.tab[data-tab="downloads"]:visible').click()
        page.get_by_text('Fallo sintético de descarga', exact=True).wait_for(state='visible')
        page.get_by_text('Este estado guardado ya no figura', exact=False).wait_for(state='visible')
        page.locator('.tab[data-tab="downloads"]:visible .tab-badge').wait_for(state='hidden')
        assert sent_count(page, 'queue') == 0
        assert errors == []


def test_hidden_view_does_not_acknowledge_until_visible():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
          window.__delay=500;window.__searchResults=[{title:'Respuesta oculta',url:'https://example.test/hidden'}];
          go('results',{q:'oculta',engine:'web',mode:'standard'});}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='oculta')")
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='oculta')")
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        page.get_by_text('Respuesta oculta', exact=True).wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='oculta')")
        assert sent_count(page, 'search') == 1
        assert errors == []


@pytest.mark.parametrize('view,fail', [('youtube', False), ('youtube', True), ('music', False), ('music', True)])
def test_paged_media_notice_from_home_opens_exact_page_or_error(view, fail):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""name => {window.__delay=500;window.__searchResults=Array.from({length:20},(_,i)=>({
          title:'Elemento '+(i+1),url:'https://youtube.com/watch?v=fixture'+(i+1)}));
          go(name,name==='music'?{q:'consulta',provider:'youtube'}:{q:'consulta'});}""", view)
        page.get_by_text('Elemento 15', exact=True).wait_for(state='visible')
        # A locator being attached is not proof that the first page survived a
        # visible paint. Complete that prerequisite before testing page 2.
        page.wait_for_function('name=>!store.get("notices",[]).some(n=>n.unread&&n.target?.view===name)', arg=view)
        if fail:
            page.evaluate('window.__failNext.youtube_search=1')
        page.get_by_role('button', name='Mostrar más resultados').click()
        page.wait_for_function("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length===2")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function('name=>store.get("notices",[]).some(n=>n.unread&&n.target?.view===name)', arg=view)
        assert page.evaluate('name=>store.get("notices",[]).find(n=>n.unread&&n.target?.view===name).target.params.resultPage', view) == 2
        page.locator('button.notice').filter(has_text='música' if view == 'music' else 'YouTube').first.click()
        expected = 'SIMULATED network uncertainty' if fail else 'Elemento 16'
        page.get_by_text(expected, exact=not fail).first.wait_for(state='visible')
        assert page.evaluate('current.params.resultPage') == 2
        assert sent_count(page, 'youtube_search') == 2
        page.wait_for_function('name=>!store.get("notices",[]).some(n=>n.unread&&n.target?.view===name)', arg=view)
        assert errors == []


def test_result_evicted_during_open_never_starts_a_new_search():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Antes',url:'https://example.test/old'}];
          go('results',{q:'carrera',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='carrera')")
        page.evaluate("""() => {const item=store.get('notices',[]).find(n=>n.unread&&n.target?.params?.q==='carrera');
          const original=savedResult;let checks=0;
          savedResult=(key)=>{if(key===item.cacheKey&&++checks===1){memo.delete(key);resultMemoTimes.delete(key);
            store.del(RESULT_CACHE_KEY);return null;}return original(key);};}""")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        assert sent_count(page, 'search') == 1
        assert errors == []


def test_reopen_on_web_restores_result_and_reads_only_after_visible():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Persistida',url:'https://example.test/persist'}];
          go('results',{q:'persistida',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='persistida')")
        page.evaluate("store.set('tab','web')")
        page.reload()
        page.wait_for_function('window.__listener && current.view === "results"')
        assert page.locator('#splash').count() == 0
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.get_by_text('Persistida', exact=True).wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='persistida')")
        assert sent_count(page, 'search') == 0
        assert errors == []


def test_reopen_with_expired_result_retains_badge_until_warning_is_visible():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Ya no guardada',url:'https://example.test/old'}];
          go('results',{q:'vieja',engine:'web',mode:'standard'});goTab('apps');}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='vieja')")
        page.evaluate("store.del(RESULT_CACHE_KEY);store.set('tab','web')")
        page.reload()
        page.wait_for_function('window.__listener && current.view === "results"')
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='vieja')")
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.get_by_text('El resultado guardado ya no está disponible', exact=False).first.wait_for(state='visible')
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.params?.q==='vieja')")
        assert sent_count(page, 'search') == 0
        assert errors == []


def test_youtube_and_music_with_same_provider_query_keep_distinct_unread_results():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Primera búsqueda',url:'https://youtube.com/watch?v=first'}];
          go('youtube',{q:'gatos music'});goTab('home');}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='youtube')")
        page.evaluate("""() => {window.__searchResults=[{title:'Segunda búsqueda',url:'https://youtube.com/watch?v=second'}];
          go('music',{q:'gatos',provider:'youtube'});}""")
        # A deliberate new search in Music must not replace YouTube's unread result.
        if page.get_by_text('Primera búsqueda', exact=True).is_visible():
            page.get_by_role('button', name='Consultar de nuevo').first.click()
        page.wait_for_function("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length===2")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='music')")
        page.locator('button.notice').filter(has_text='YouTube').first.click()
        page.get_by_text('Primera búsqueda', exact=True).wait_for(state='visible', timeout=5000)
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='music')")
        assert sent_count(page, 'youtube_search') == 2
        assert errors == []


def test_legacy_youtube_notice_recovers_its_original_page_from_cache_key():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=Array.from({length:20},(_,i)=>({
          title:'Antiguo '+(i+1),url:'https://youtube.com/watch?v=old'+(i+1)}));go('youtube',{q:'antigua'});}""")
        page.get_by_text('Antiguo 15', exact=True).wait_for(state='visible')
        page.get_by_role('button', name='Mostrar más resultados').click()
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='youtube')")
        page.evaluate("""() => {const notices=store.get('notices',[]),n=notices.find(x=>x.unread&&x.target?.view==='youtube');
          const old='youtube_search'+JSON.stringify({q:'antigua',page:2});
          const records=store.get(RESULT_CACHE_KEY,{});records[old]=records[n.cacheKey];delete records[n.cacheKey];store.set(RESULT_CACHE_KEY,records);
          memo.set(old,memo.get(n.cacheKey));resultMemoTimes.set(old,resultMemoTimes.get(n.cacheKey));memo.delete(n.cacheKey);resultMemoTimes.delete(n.cacheKey);
          n.cacheKey=old;delete n.target.params.resultPage;store.set('notices',notices);bus.emit('notices');}""")
        page.locator('button.notice').filter(has_text='YouTube').first.click()
        page.get_by_text('Antiguo 16', exact=True).wait_for(state='visible', timeout=5000)
        assert page.evaluate('current.params.resultPage') == 2
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='youtube')")
        assert sent_count(page, 'youtube_search') == 2
        assert errors == []


def test_web_and_news_with_same_query_do_not_replace_unread_results():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=500;window.__searchResults=[{title:'Desde Web',url:'https://example.test/web'}];
          go('results',{q:'misma',engine:'news'});goTab('home');}""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='results')")
        page.evaluate("""() => {window.__searchResults=[{title:'Desde Noticias',url:'https://example.test/news'}];
          go('news',{q:'misma'});}""")
        if page.get_by_text('Desde Web', exact=True).is_visible():
            page.get_by_role('button', name='Consultar de nuevo').first.click()
        page.wait_for_function("window.__sentUpdates.filter(x=>x.payload?.op==='search').length===2")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='news')")
        page.locator('.tab[data-tab="web"]:visible').click()
        page.get_by_text('Desde Web', exact=True).wait_for(state='visible', timeout=5000)
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='news')")
        assert sent_count(page, 'search') == 2
        assert errors == []


def test_more_than_thirty_unread_notices_are_not_silently_discarded():
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {for(let i=0;i<35;i++) notice('Respuesta sintética '+i,'ok',
          {category:'ai',target:{view:'chat',params:{space:'fixture-'+i}}});}""")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.category==='ai').length") == 35
        assert '35 sin leer' in page.locator('.tab[data-tab="ai"]:visible').get_attribute('aria-label')
        page.evaluate("""() => {for(let i=0;i<40;i++) notice('Aviso local leído '+i,'info',{local:true});}""")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&n.category==='ai').length") == 35
        assert page.evaluate("store.get('notices',[]).filter(n=>!n.unread).length") == 30
        assert errors == []


VIEW_REQUESTS = [
    ('reader', 'read', {'url': 'https://example.test/article'}, 'Contenido sintético del lector', 'web'),
    ('transcript', 'action', {'url': 'https://youtube.com/watch?v=fixture'}, 'smoke test result', 'apps'),
    ('music_item', 'music_item', {'id': 'album-1'}, 'Jazz track', 'apps'),
]


@pytest.mark.parametrize('view,op,params,success,category', VIEW_REQUESTS)
@pytest.mark.parametrize('failed', [False, True])
def test_page_transcript_and_music_item_notify_and_reopen(view, op, params, success, category, failed):
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('op => { window.__delay=700; if (op) window.__failNext[op]=1; }', op if failed else '')
        page.evaluate('v => go(v.view,v.params)', {'view': view, 'params': params})
        page.wait_for_function('op => window.__sentUpdates.some(x=>x.payload?.op===op)', arg=op)
        page.locator('.tab[data-tab="home"]:visible').click()
        assert page.locator(f'.tab[data-tab="{category}"]:visible .tab-badge').is_hidden()
        page.wait_for_function('v => store.get("notices",[]).some(n=>n.unread&&n.target?.view===v)', arg=view, timeout=6000)
        page.locator(f'.tab[data-tab="{category}"]:visible').click()
        assert page.evaluate('current.view') == view
        page.get_by_text('SIMULATED network uncertainty' if failed else success, exact=False).first.wait_for(state='visible')
        page.wait_for_function('v => !store.get("notices",[]).some(n=>n.unread&&n.target?.view===v)', arg=view)
        assert sent_count(page, op) == 1
        assert errors == []


@pytest.mark.parametrize('view,op,params,success,category', VIEW_REQUESTS)
def test_returning_to_pending_page_does_not_repeat_request(view, op, params, success, category):
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('window.__delay=900')
        request = {'view': view, 'params': params}
        page.evaluate('v => {go(v.view,v.params);goTab("home");go(v.view,v.params)}', request)
        page.get_by_text(success, exact=False).first.wait_for(state='visible')
        assert sent_count(page, op) == 1
        assert not page.evaluate('v => store.get("notices",[]).some(n=>n.unread&&n.target?.view===v)', view)
        assert errors == []


def test_transcript_reply_after_reopening_is_delivered_without_resending():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""() => {window.__delay=60000;go('transcript',{url:'https://youtube.com/watch?v=fixture'});goTab('home')}""")
        rid = page.evaluate("Object.entries(bridge.pending()).find(([,m])=>m.op==='action'&&m.ctx?.kind==='view-result')[0]")
        page.reload()
        page.wait_for_function('window.__listener && cache.at > 0')
        assert page.evaluate('id => !!bridge.pending()[id]', rid)
        page.evaluate("""id => window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:true,
          data:{title:'Transcripción sintética',text:'Texto recibido tras reabrir'}}})""", rid)
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread&&n.target?.view==='transcript')")
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.locator('.tab[data-tab="apps"]:visible').tap()
        page.get_by_text('Texto recibido tras reabrir', exact=True).wait_for(state='visible')
        assert sent_count(page, 'action') == 0
        page.wait_for_function("!store.get('notices',[]).some(n=>n.unread&&n.target?.view==='transcript')")
        assert errors == []


@pytest.mark.parametrize('view,op,category', [
    ('results', 'search', 'web'), ('reader', 'read', 'web'),
    ('transcript', 'action', 'apps'), ('music_item', 'music_item', 'apps'),
    ('translate', 'translate', 'apps'), ('weather', 'weather', 'apps'),
    ('chat', 'ask', 'ai'), ('images', 'image', 'ai'), ('coder', 'code', 'ai'),
])
def test_local_timeout_is_history_not_an_unread_reply(view, op, category):
    with browser_fixture('es', {'width': 1280, 'height': 800}) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate('window.__delay=60000')
        if view == 'results':
            page.evaluate("go('results',{q:'sin respuesta',engine:'web',mode:'standard'})")
        elif view == 'reader':
            page.evaluate("go('reader',{url:'https://example.test/article'})")
        elif view == 'transcript':
            page.evaluate("go('transcript',{url:'https://youtube.com/watch?v=synthetic'})")
        elif view == 'music_item':
            page.evaluate("go('music_item',{id:'album-1'})")
        elif view == 'chat':
            page.evaluate("go('chat',{space:'agent'});ask('agent','Pregunta sintética')")
        elif view == 'images':
            page.evaluate("go('images');createImage('Imagen sintética')")
        elif view == 'coder':
            page.evaluate("go('coder');coderSend('Proyecto sintético')")
        else:
            page.evaluate('v=>go(v)', view)
            if view == 'translate':
                page.locator('#view textarea').fill('Hola')
                page.get_by_role('button', name='Traducir', exact=True).click()
            else:
                page.locator('#view input').first.fill('Havana')
                page.get_by_role('button', name='Consultar').first.click()
        rid = page.evaluate('op=>Object.entries(bridge.pending()).find(([,m])=>m.op===op)[0]', op)
        page.evaluate("goTab('home')")
        # This exercises the same local settlement path used by timeout/repair,
        # not an update from the messenger and not a second request.
        page.evaluate("id=>bridge.settle({re:id,ok:false,error:t('no_answer')})", rid)
        page.wait_for_function("v=>store.get('notices',[]).some(n=>n.target?.view===v && n.text===t('notification_local_wait_ended'))", arg=view)
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread)")
        assert page.locator(f'.tab[data-tab="{category}"]:visible .tab-badge').is_hidden()
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').is_hidden()
        page.locator('button.notice').filter(has_text='La app dejó de esperar sin recibir respuesta').first.click()
        assert page.evaluate('current.view') == view
        page.locator('#view').get_by_text('No llegó respuesta.', exact=False).first.wait_for(state='visible')
        assert sent_count(page, op) == 1
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_home_marks_only_general_notices_shown_in_the_viewport(engine):
    with browser_fixture('es', {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.emulate_media(reduced_motion='reduce')
        sent_before = page.evaluate('window.__sentUpdates.length')
        page.evaluate("""() => {
          goTab('ai');
          for (const label of ['A', 'B', 'C']) notice('Aviso sintético ' + label, 'info');
          goTab('home');
        }""")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&!n.target&&n.text.startsWith('Aviso sintético')).length") == 3
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').inner_text() == '3'
        # One row is almost entirely below the viewport: merely opening Home or
        # clipping a small edge cannot count it as seen.
        def show_fraction(fraction):
            page.evaluate("""fraction => {
              const root=document.querySelector('#view'), bounds=root.getBoundingClientRect();
              const row=[...root.querySelectorAll('.notice')].find(e=>e.textContent.includes('Aviso sintético C'));
              const rect=row.getBoundingClientRect();
              root.scrollTop += rect.top - bounds.bottom + rect.height * fraction;
            }""", fraction)

        show_fraction(.2)
        page.wait_for_timeout(120)
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&!n.target&&n.text.startsWith('Aviso sintético')).length") == 3
        show_fraction(.7)
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso sintético C').unread")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.unread&&!n.target&&n.text.startsWith('Aviso sintético')).map(n=>n.text)") == ['Aviso sintético B', 'Aviso sintético A']
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').inner_text() == '2'
        assert page.evaluate('window.__sentUpdates.length') == sent_before
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_general_notice_arriving_on_home_stays_unread_until_seen(engine):
    with browser_fixture('es', {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.emulate_media(reduced_motion='reduce')
        page.evaluate("""() => { goTab('home'); notice('Aviso fuera de pantalla','info');
          notice('Espera local','info',{local:true}); }""")
        assert page.evaluate("store.get('notices',[]).find(n=>n.text==='Aviso fuera de pantalla').unread")
        assert not page.evaluate("store.get('notices',[]).find(n=>n.text==='Espera local').unread")
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').inner_text() == '1'
        # Hiding the WebView must not acknowledge a row that scrolls into view.
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{value:'hidden',configurable:true});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        page.locator('.notice').filter(has_text='Aviso fuera de pantalla').scroll_into_view_if_needed()
        page.wait_for_timeout(120)
        assert page.evaluate("store.get('notices',[]).find(n=>n.text==='Aviso fuera de pantalla').unread")
        page.evaluate("""() => {Object.defineProperty(document,'visibilityState',{value:'visible',configurable:true});
          document.dispatchEvent(new Event('visibilitychange'));}""")
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso fuera de pantalla').unread")
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').is_hidden()
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_general_notice_covered_by_sheet_is_read_after_closing_it(engine):
    with browser_fixture('es', {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.emulate_media(reduced_motion='reduce')
        page.evaluate("goTab('home');notice('Aviso de referencia','info')")
        page.wait_for_timeout(400)
        page.locator('.notice').filter(has_text='Aviso de referencia').scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso de referencia').unread")
        page.evaluate("openSheet('Panel sintético',[h('p',{},'Cubierto')]);notice('Aviso cubierto','info')")
        # Put the notice in the viewport while the sheet still obscures it.
        page.evaluate("""() => {const root=document.querySelector('#view'),box=root.getBoundingClientRect();
          const row=[...root.querySelectorAll('.notice')].find(e=>e.textContent.includes('Aviso cubierto'));
          const rect=row.getBoundingClientRect();root.scrollTop += rect.top-box.top-(box.height-rect.height)/2;}""")
        page.wait_for_timeout(150)
        assert page.evaluate("store.get('notices',[]).find(n=>n.text==='Aviso cubierto').unread")
        page.evaluate('closeSheet()')
        page.wait_for_function("document.querySelector('#sheet').hidden && !store.get('notices',[]).find(n=>n.text==='Aviso cubierto').unread")
        # A new general notice appearing in the part already on screen is read.
        page.evaluate("notice('Aviso ya visible','info')")
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso ya visible').unread")
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').is_hidden()
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_general_notice_becomes_visible_after_resize(engine):
    with browser_fixture('es', {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.locator('#splash').wait_for(state='detached', timeout=8000)
        page.emulate_media(reduced_motion='reduce')
        page.evaluate("goTab('ai');notice('Aviso al cambiar tamaño','info');goTab('home')")
        page.wait_for_timeout(400)
        assert page.evaluate("store.get('notices',[]).find(n=>n.text==='Aviso al cambiar tamaño').unread")
        page.set_viewport_size({'width': 320, 'height': 1600})
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text==='Aviso al cambiar tamaño').unread")
        assert errors == []
