"""Whole-app styles and transport observations, using an entirely synthetic bridge."""
from __future__ import annotations

import time

import pytest

from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


@pytest.mark.parametrize('language,engine,width', [
    ('es', 'webkit', 390), ('en', 'chromium', 320),
    ('en', 'webkit', 1280), ('es', 'chromium', 1280),
])
def test_styles_cover_navigation_icons_brands_banners_and_views(language, engine, width):
    with browser_fixture(language, {'width': width, 'height': 844}, engine=engine, touch=width < 900) as (page, errors):
        page.wait_for_function('cache.at > 0')
        assert page.evaluate("VISUAL_STYLES.length") == 13
        # The source art must not change color, filter or require network just
        # because the surrounding frame adopts a look.
        official = page.evaluate("brandMark('gemini').querySelector('img').getAttribute('src')")
        assert official.startswith('data:image/')
        banners = {}
        for style in ('michiflin', 'material', 'ocean'):
            for theme in ('light', 'dark'):
                page.evaluate("x=>{prefs.style=x.style;prefs.theme=x.theme;prefs.accent='style';applyLook()}", {'style': style, 'theme': theme})
                for tab in ('home', 'ai', 'web', 'apps', 'downloads', 'settings'):
                    page.evaluate('tab=>goTab(tab)', tab)
                    assert page.evaluate('document.documentElement.dataset.style') == style
                    assert page.evaluate('document.documentElement.dataset.theme') == theme
                    nav = page.locator(f'.tab[data-tab="{tab}"]:visible')
                    if tab == 'settings' and width < 900:
                        icon = page.locator('#gear .ic')
                    else:
                        assert 'on' in (nav.get_attribute('class') or '')
                        icon = nav.locator('.ic')
                    assert icon.is_visible()
                    assert icon.evaluate("e=>getComputedStyle(e).strokeLinecap") == 'round'
                    assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
                page.evaluate("""() => { const old=document.querySelector('#look-test-brand'); if(old) old.remove();
                  const brand=brandMark('gemini');brand.id='look-test-brand';document.body.appendChild(brand); }""")
                mark = page.locator('#look-test-brand')
                assert mark.locator('img').get_attribute('src') == official
                assert mark.locator('img').evaluate('e=>getComputedStyle(e).filter') == 'none'
                radius = float(mark.evaluate('e=>parseFloat(getComputedStyle(e).borderTopLeftRadius)'))
                if style == 'material':
                    assert radius >= 16
                elif style == 'ocean':
                    assert radius >= 13
                page.evaluate("document.querySelector('#look-test-brand').remove();goTab('home')")
                colors = []
                for index in range(3):
                    page.locator('.home-banner-dot').nth(index).click()
                    colors.append(page.locator('.home-banner-slot > *').evaluate('e=>getComputedStyle(e).backgroundImage'))
                if theme == 'dark':
                    banners[style] = colors
            if width == 390:
                screenshot_if_requested(page, 'complete-look-' + style)
        # All three regular home banners, including rates, reflect each look.
        for style in ('material', 'ocean'):
            assert all(a != b for a, b in zip(banners[style], banners['michiflin'])), (style, banners)
        assert errors == []


@pytest.mark.parametrize('engine,width', [('webkit', 320), ('chromium', 1280)])
def test_system_scheme_matches_light_and_dark_in_all_looks(engine, width):
    with browser_fixture('en', {'width': width, 'height': 844}, engine=engine, touch=width < 900) as (page, errors):
        page.wait_for_function('cache.at > 0')
        variables = """() => {const s=getComputedStyle(document.documentElement);return ['--bg','--surface','--text','--muted','--accent'].map(k=>s.getPropertyValue(k).trim())}"""
        for style in ('michiflin', 'material', 'ocean'):
            for scheme in ('light', 'dark'):
                page.emulate_media(color_scheme=scheme)
                page.evaluate("v=>{prefs.style=v;prefs.theme='system';prefs.accent='style';applyLook()}", style)
                actual = page.evaluate(variables)
                page.evaluate('v=>{prefs.theme=v;applyLook()}', scheme)
                assert actual == page.evaluate(variables), (style, scheme, actual)
        page.evaluate("go('settings',{section:'connection'})")
        assert page.get_by_text('Last sync reply:', exact=False).count() >= 1
        assert page.get_by_text('no extra data is sent', exact=False).count() >= 1
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('cached', [True, False])
def test_splash_stops_blocking_while_slow_sync_continues(engine, cached):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        # Ten-second synthetic sync, with either a snapshot or no snapshot
        # (the introduction was already skipped by the fixture). No real account.
        if not cached:
            page.evaluate("store.del('cache')")
        page.add_init_script("Object.defineProperty(window,'__delay',{configurable:true,get(){return 10000},set(_v){}})")
        started = time.monotonic()
        page.reload(wait_until='domcontentloaded')
        page.locator('#splash').wait_for(state='detached', timeout=3500)
        elapsed = time.monotonic() - started
        print(f'{engine} {"con caché" if cached else "primer sync sin caché"}: {elapsed:.2f} s, respuesta simulada pendiente')
        assert elapsed < 3.5
        assert bool(page.evaluate('cache.at')) is cached
        assert page.evaluate("Object.values(bridge.pending()).some(m=>m.op==='hello')")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='hello').length") == 1
        page.locator('.tab[data-tab="apps"]:visible').click()
        assert page.evaluate('current.view') == 'apps'
        assert errors == []


def test_local_timing_reports_only_received_results_without_extra_requests():
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate("""() => {window.__delay=750;window.__searchResults=[{title:'Sólo ejemplo',url:'https://example.test'}];
          go('results',{q:'ejemplo',engine:'web',mode:'standard'});}""")
        page.get_by_text('Sólo ejemplo', exact=True).wait_for(state='visible')
        timing = page.evaluate('bridge.timings.request')
        assert timing['op'] == 'search' and 600 <= timing['ms'] < 3000
        before = page.evaluate('window.__sentUpdates.length')
        page.evaluate("go('settings',{section:'connection'})")
        # Sync tools live in a collapsed section now; open it the way a user would.
        page.get_by_text('Última respuesta (Búsqueda):', exact=False).wait_for(state='visible')
        assert page.get_by_text('no se envían datos adicionales.', exact=False).count() >= 1
        assert page.evaluate('window.__sentUpdates.length') == before
        assert not page.evaluate("JSON.stringify(bridge.timings).includes('ejemplo')")
        assert errors == []


def test_timing_distinguishes_received_errors_from_local_timeouts():
    with browser_fixture('en', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate('window.__delay=60000')
        rid = page.evaluate("bridge.request('search',{q:'private synthetic example'},null).id")
        page.evaluate("""id=>window.__listener({serial:bridge.serial+1,payload:{t:'res',re:id,ok:false,error:'Synthetic failure'}})""", rid)
        assert page.evaluate('bridge.timings.request.ok') is False
        observed = page.evaluate('bridge.timings.request')
        timeout_id = page.evaluate("bridge.request('weather',{city:'synthetic'},null).id")
        page.evaluate("id=>bridge.settle({re:id,ok:false,error:'local timeout'})", timeout_id)
        assert page.evaluate('bridge.timings.request') == observed
        page.evaluate("go('settings',{section:'connection'})")
        assert page.get_by_text('Last error (Search):', exact=False).count() == 1
        assert not page.evaluate("JSON.stringify(bridge.timings).includes('private synthetic example')")
        page.reload()
        page.wait_for_function('!!window.__listener')
        assert page.evaluate('bridge.timings.request') is None
        assert errors == []


def test_background_rates_yield_to_sync_and_deliberate_tasks():
    with browser_fixture('es', {'width': 390, 'height': 844}) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        # Only quiet updates wait out the host's declared spacing (10 s if not
        # exposed); user requests retain their existing send behavior.
        assert page.evaluate('quietRefreshBusy()')
        page.evaluate("() => { bridge.lastSentAt=Date.now()-11000; }")
        assert not page.evaluate('quietRefreshBusy()')
        page.evaluate("() => { window.webxdc.sendUpdateInterval=20000; }")
        assert page.evaluate('quietRefreshBusy()')
        page.evaluate("() => { window.webxdc.sendUpdateInterval=0; }")
        assert not page.evaluate('quietRefreshBusy()')
        page.evaluate("() => { window.webxdc.sendUpdateInterval=null; bridge.lastSentAt=Date.now()-1000; }")
        assert page.evaluate('quietRefreshBusy()')  # unavailable: use the documented 10 s default
        page.evaluate("() => { window.webxdc.sendUpdateInterval=0; syncing = new Promise(()=>{}); }")
        assert page.evaluate('quietRefreshBusy()')
        page.evaluate("syncing = null; window.__delay=60000; go('chat',{space:'agent'}); ask('agent','sólo sintética')")
        assert page.evaluate('quietRefreshBusy()')
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='ask').length") == 1
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_text_contrast_across_styles_accents_and_light_dark(engine):
    """Check key foregrounds against conservative solid/gradient endpoint backgrounds."""
    with browser_fixture('en', {'width': 1280, 'height': 800}, engine=engine) as (page, errors):
        page.wait_for_function('cache.at > 0')
        measurements = page.evaluate("""() => {
          const noMotion=document.createElement('style');
          noMotion.textContent='*{transition:none!important;animation:none!important}';document.head.appendChild(noMotion);
          const button=document.createElement('button');button.className='btn primary';document.body.appendChild(button);
          function rgba(color) {
            const e=document.createElement('span');e.style.color=color;document.body.appendChild(e);
            const value=getComputedStyle(e).color.match(/[\\d.]+/g).map(Number);e.remove();return value;
          }
          function lum(rgb) {return rgb.slice(0,3).map(n=>{n/=255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4})
            .reduce((sum,x,i)=>sum+x*[.2126,.7152,.0722][i],0);}
          function contrast(a,b){const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
          function blend(f,b){const a=f[3]??1;return b.map((value,i)=>i<3?f[i]*a+value*(1-a):1);}
          const rows=[];
          for(const style of ['michiflin','material','ocean'])
            for(const theme of ['light','dark'])
              for(const accent of ['style','teal','violet','blue','rose']) {
                prefs.style=style;prefs.theme=theme;prefs.accent=accent;applyLook();
                const root=getComputedStyle(document.documentElement), nav=getComputedStyle(document.querySelector('.rail .tab.on'));
                const surface=rgba(root.getPropertyValue('--surface'));
                const surface2=rgba(root.getPropertyValue('--surface-2'));
                const soft=rgba(root.getPropertyValue('--accent-soft'));
                // A selected rail item is tinted, on top of a potentially tinted rail.
                const rail=style==='michiflin'?surface:blend(soft,surface);
                const selected=blend(soft,rail);
                const btn=getComputedStyle(button), image=btn.backgroundImage;
                const stops=[...image.matchAll(/rgba?\\([^)]*\\)/g)].map(m=>rgba(m[0]));
                if(!stops.length) stops.push(rgba(btn.backgroundColor));
                rows.push({style,theme,accent,
                  text:contrast(rgba(root.getPropertyValue('--text')),surface),
                  muted:Math.min(contrast(rgba(root.getPropertyValue('--muted')),surface2),contrast(rgba(root.getPropertyValue('--muted')),rail)),
                  nav:contrast(rgba(nav.color),selected),
                  button:Math.min(...stops.map(bg=>contrast(rgba(btn.color),bg)))});
              }
          button.remove();noMotion.remove();return rows;
        }""")
        assert len(measurements) == 30
        for row in measurements:
            assert min(row['text'], row['muted'], row['nav'], row['button']) >= 4.5, row
        assert errors == []


def test_quiet_rates_and_weather_wait_without_delaying_user_requests():
    """Run the actual scheduled callbacks against a synthetic Delta bridge."""
    with browser_fixture('es', {'width': 390, 'height': 844}, engine='webkit', touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0 && !syncing')
        page.evaluate("""() => {
          const element=document.createElement('div');document.body.appendChild(element);
          const original=window.setTimeout, checks={};
          window.setTimeout=(fn,ms,...args)=>{
            if(ms===8000 || ms===90000) {checks[ms]=fn;return 0;}
            return original(fn,ms,...args);
          };
          watchVisibleRates(element);watchHomeWeather(element);window.setTimeout=original;
          window.__quietChecks=checks;window.__delay=60000;
          store.set('rates',{rates:{USD:390},at:now()-1000,refresh_seconds:120});
          store.set('weatherHome',{city:'Fixture',auto:true,next_at:now()-1000,last:null});
          store.set('weatherHomeSyncAt',0);
          window.webxdc.sendUpdateInterval=20000;bridge.lastSentAt=Date.now()-11000;
        }""")
        before = page.evaluate("""() => ({rates:window.__sentUpdates.filter(x=>x.payload?.op==='rates').length,
          hello:window.__sentUpdates.filter(x=>x.payload?.op==='hello').length})""")
        page.evaluate('() => {window.__quietChecks[8000]();window.__quietChecks[90000]();}')
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='rates').length") == before['rates']
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='hello').length") == before['hello']
        # A deliberate request is sent immediately even inside that interval.
        rid = page.evaluate("""() => bridge.request('search',{q:'synthetic'},
          {kind:'view-result',key:'synthetic',target:{view:'results',params:{q:'synthetic'}},category:'web',label:'notification_search'}).id""")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert page.evaluate('Date.now()-bridge.lastSentAt < 2000')
        # Pending visible work blocks both quiet callbacks even after the interval.
        page.evaluate('() => {bridge.lastSentAt=Date.now()-21000;window.__quietChecks[8000]();window.__quietChecks[90000]();}')
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='rates').length") == before['rates']
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='hello').length") == before['hello']
        page.evaluate('id=>bridge.forget(id)', rid)
        page.evaluate('() => {window.__quietChecks[8000]();window.__quietChecks[90000]();}')
        # The rate refresh now starts; its send postpones the weather sync.
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='rates').length") == before['rates'] + 1
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='hello').length") == before['hello']
        # Another deliberate request is not made to wait behind the background rate.
        second = page.evaluate("""() => bridge.request('search',{q:'second synthetic'},
          {kind:'view-result',key:'synthetic-2',target:{view:'results',params:{q:'second synthetic'}},category:'web',label:'notification_search'}).id""")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 2
        page.evaluate("""id => {bridge.forget(id);const rate=Object.entries(bridge.pending()).find(([,m])=>m.op==='rates');
          if(rate)bridge.forget(rate[0]);}""", second)
        page.wait_for_function('!sharedRateRefresh')
        page.evaluate('() => {bridge.lastSentAt=Date.now()-21000;window.__quietChecks[90000]();}')
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='hello').length") == before['hello'] + 1
        assert errors == []
