"""Synthetic UI tests for reply-only badges, local art and appearance controls."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_visible_answer_never_creates_an_unread_badge(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=800; go('chat',{space:'agent'})")
        page.locator("#view textarea").fill("Sólo en esta vista")
        page.locator("#view .composer button.primary").tap()
        ai = page.locator('.tab[data-tab="ai"]:visible')
        assert ai.locator(".tab-badge").is_hidden()
        page.get_by_text("SIMULATED agent answer", exact=False).first.wait_for(state="visible")
        assert ai.locator(".tab-badge").is_hidden()
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread && n.target?.view==='chat')")
        assert errors == []


def test_hidden_document_counts_as_away_even_when_same_chat_is_selected():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=700; go('chat',{space:'agent'})")
        page.locator("#view textarea").fill("En segundo plano")
        page.locator("#view .composer button.primary").click()
        page.evaluate("""() => { Object.defineProperty(document,'visibilityState',{value:'hidden', configurable:true});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread && n.target?.view==='chat')")
        assert page.locator('.tab[data-tab="ai"]:visible .tab-badge').inner_text() == "1"
        page.evaluate("""() => { Object.defineProperty(document,'visibilityState',{value:'visible', configurable:true});
          document.dispatchEvent(new Event('visibilitychange')); }""")
        assert page.locator('.tab[data-tab="ai"]:visible .tab-badge').is_hidden()
        assert errors == []


def test_download_progress_is_visible_in_queue_but_not_a_reply_badge():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => setJobs([{id:17,title:'Archivo sintético',kind:'file',status:'downloading',size:1000,done:300,created:Date.now()/1000}])""")
        tab = page.locator('.tab[data-tab="downloads"]:visible')
        assert tab.locator(".tab-badge").is_hidden()
        tab.click()
        assert page.locator('.job.downloading').get_by_text("Archivo sintético").is_visible()
        assert page.locator('.job.downloading .bar').count() or page.locator('.job.downloading progress').count()
        page.evaluate("""() => bridge.pushes.job({id:17,title:'Archivo sintético',kind:'file',status:'done',size:1000,done:1000,created:Date.now()/1000})""")
        assert tab.locator(".tab-badge").is_hidden()  # default Downloads subsection is currently visible
        assert not page.evaluate("store.get('notices',[]).some(n=>n.unread && n.target?.view==='downloads')")
        page.get_by_role("tab", name="Herramientas de enlaces").click() if page.get_by_role("tab", name="Herramientas de enlaces").count() else page.evaluate("go('downloads',{section:'links'})")
        page.evaluate("""() => { setJobs([{id:18,title:'Segundo',kind:'file',status:'downloading',size:20,done:10,created:Date.now()/1000}]);
          bridge.pushes.job({id:18,title:'Segundo',kind:'file',status:'done',size:20,done:20,created:Date.now()/1000}); }""")
        assert tab.locator(".tab-badge").inner_text() == "1"  # links are not the jobs view
        assert errors == []


@pytest.mark.parametrize("engine,width", [("chromium", 390), ("webkit", 320), ("chromium", 1280)])
def test_four_styles_light_dark_accent_persist_and_fit(engine, width):
    with browser_fixture("es", {"width": width, "height": 844}, engine=engine, touch=width < 900) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("go('settings')")
        styles = page.get_by_label("Estilo visual")
        accents = page.get_by_label("Color de acento")
        assert styles.locator("option").count() == 4
        assert accents.locator("option").count() == 5
        # Narrow iPhone screens must show the entire setting label, not hide it
        # underneath the system/light/dark segmented control.
        label = page.get_by_text("Apariencia", exact=True)
        assert label.evaluate("e => e.scrollWidth <= e.clientWidth")
        for style in ("michiflin", "material", "pixel", "ocean"):
            styles.select_option(style)
            for theme in ("light", "dark"):
                page.evaluate("theme => {prefs.theme=theme;applyLook();}", theme)
                for accent in ("style", "teal", "violet", "blue", "rose"):
                    accents.select_option(accent)
                    actual = page.evaluate("""() => {
                      const d = document.documentElement, css = getComputedStyle(d), main = document.querySelector('#view');
                      function rgb(s) { const p = document.createElement('span'); p.style.color=s; document.body.appendChild(p);
                        const v=getComputedStyle(p).color.match(/[\\d.]+/g).slice(0,3).map(Number); p.remove(); return v; }
                      function lum(v) { return v.map(n=>{n/=255;return n<=.04045?n/12.92:Math.pow((n+.055)/1.055,2.4)}).reduce((a,x,i)=>a+x*[.2126,.7152,.0722][i],0); }
                      function contrast(a,b) { a=lum(rgb(a)); b=lum(rgb(b)); return (Math.max(a,b)+.05)/(Math.min(a,b)+.05); }
                      return {style:d.dataset.style, theme:d.dataset.theme, accent:d.dataset.accent,
                        ratio:contrast(css.getPropertyValue('--text'),css.getPropertyValue('--surface')),
                        muted:contrast(css.getPropertyValue('--muted'),css.getPropertyValue('--surface')),
                        wide:main.scrollWidth>main.clientWidth};
                    }""")
                    assert (actual["style"], actual["theme"], actual["accent"]) == (style, theme, accent)
                    assert actual["ratio"] >= 4.5 and actual["muted"] >= 4.5, actual
                    assert not actual["wide"], actual
            if width == 390:
                screenshot_if_requested(page, f"style-{style}-dark")
        # Select through the actual UI; local prefs survive a reload of this instance.
        styles.select_option("material")
        accents.select_option("rose")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("document.documentElement.dataset.style==='material'")
        assert page.evaluate("document.documentElement.dataset.accent") == "rose"
        assert errors == []


def test_real_weather_codes_drive_offline_art_and_reduced_motion():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        assert page.evaluate("""() => [null,0,1,2,3,45,48,51,63,82,71,77,85,95,96,99,100].map(code=>weatherScene({code}))""") == [
            "unknown", "clear", "partly", "partly", "cloudy", "fog", "fog", "rain", "rain", "rain", "snow", "snow", "snow", "storm", "storm", "storm", "unknown"]
        assert page.evaluate("[true, false, ' ', '1.0', {}, []].map(code=>weatherScene({code}))") == ["unknown"] * 6
        assert page.evaluate("weatherIcon(0, null)") == "info"
        assert page.evaluate("weatherBannerArt({code:0,is_day:1,is_day_known:false}).querySelector('.scene-orb') === null")
        page.evaluate("""() => { const w=weatherHome(); w.last={at:now(),data:{place:{label:'Havana'},current:{code:63,is_day:1,temp:28,description:'Lluvia real'}}};
          store.set('weatherHome',w); bus.emit('weather_home'); }""")
        before = page.evaluate("window.__sentUpdates.length")
        page.locator(".home-banner-dot").nth(2).click()
        banner = page.locator(".weather-banner")
        assert banner.get_attribute("data-weather-scene") == "rain"
        assert banner.locator(".scene-precip-rain").count() == 1
        assert banner.locator(".scene-orb").count() == 0
        assert banner.locator(".weather-scene").get_attribute("aria-hidden") == "true"
        assert "Lluvia real" in banner.inner_text()
        assert page.evaluate("window.__sentUpdates.length") == before  # changing art adds no request
        page.emulate_media(reduced_motion="reduce")
        assert banner.locator(".scene-precip-rain").evaluate("e=>getComputedStyle(e).animationName") == "none"
        page.locator(".home-banner-dot").nth(1).click()
        assert page.locator(".rate-banner").evaluate("e=>getComputedStyle(e,'::after').animationName") == "none"
        page.locator(".home-banner-dot").nth(0).click()
        assert page.locator(".hero-art .ic:not(.pixel-mascot)").evaluate("e=>getComputedStyle(e).animationName") == "none"
        page.evaluate("""() => { const w=weatherHome(); w.last.data.current={code:0,is_day:0,temp:23,description:'Despejado'};
          store.set('weatherHome',w); bus.emit('weather_home'); }""")
        page.locator(".home-banner-dot").nth(2).click()
        assert page.locator(".weather-scene.at-night .scene-orb").count() == 1
        assert page.locator(".weather-scene .scene-precip-rain").count() == 0
        assert errors == []


def test_offline_authentic_logos_and_neutral_unknown():
    with browser_fixture("en", {"width": 390, "height": 844}) as (page, errors):
        values = page.evaluate("""() => {
          const examples = ['reflection/beam-free','recursive/flint-v1.0:free','poolside/laguna','cohere/north-mini','kilo-auto/free',
                            'openai/gpt-oss','qwen/qwen3','unknown/model'];
          return examples.map(id => {const name=brandOf({id,title:id},{key:'cloudflare'}), mark=brandMark(name);
            document.body.appendChild(mark); return {name, generic:mark.classList.contains('brand-generic'), src:mark.querySelector('img')?.src || ''};});
        }""")
        assert [v["name"] for v in values] == ["reflection", "recursive", "poolside", "cohere", "kilo", "openai", "qwen", ""]
        assert all(v["src"].startswith("data:image/") and not v["generic"] for v in values[:-1])
        assert values[-1]["generic"] and not values[-1]["src"]
        page.wait_for_function("Array.from(document.querySelectorAll('.brand-official img')).every(i=>i.complete && i.naturalWidth>0)")
        assert errors == []


@pytest.mark.parametrize("language,engine", [("es", "webkit"), ("en", "chromium")])
def test_graphical_saved_live_discloses_control_and_shows_explicit_actions(language, engine):
    from tests.test_webxdc_browser import install_live_frame_fixture
    with browser_fixture(language, {"width": 390, "height": 844}, engine=engine, touch=engine == "webkit") as (page, errors):
        install_live_frame_fixture(page)
        assert page.evaluate("window.liveWeb.browser") == "graphical_saved"
        hint = "Playwright restaura" if language == "es" else "Playwright restores"
        assert page.get_by_text(hint, exact=False).is_visible()
        page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").click()
        page.wait_for_function("document.querySelector('.live-screen')?.naturalWidth === 480")
        assert page.evaluate("window.__testLiveActions.some(x=>x.t==='start' && x.browser==='graphical_saved')")
        assert page.get_by_role("button", name="Guardar sesión" if language == "es" else "Save session").is_visible()
        assert page.get_by_role("button", name="Descargar este video" if language == "es" else "Download this video").is_visible()
        assert page.get_by_role("button", name="Cerrar ventana actual" if language == "es" else "Close current window").is_visible()
        page.evaluate("window.liveWeb.onMessage({t:'error',seq:1,error:'multiple_pages'})")
        warning = "Cierra las demás pestañas" if language == "es" else "Close the other tabs"
        assert page.get_by_text(warning, exact=False).is_visible()
        assert errors == []
