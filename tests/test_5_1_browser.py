"""Mobile and bilingual WebXDC integration against a synthetic Delta bridge."""
from __future__ import annotations


import pytest

from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


@pytest.mark.parametrize("language,width", [("es", 320), ("en", 375)])
def test_mobile_agent_layout_settings_and_maintenance_are_explicit(language, width):
    with browser_fixture(language, {"width": width, "height": 720}) as (page, errors):
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
        page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
        page.locator(".agent-picker").wait_for(state="visible")
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        # Neither the two mode buttons nor their text overlap on small screens.
        boxes = page.locator('.agent-mode [role="tab"]').evaluate_all("nodes => nodes.map(n => {let r=n.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}})")
        assert boxes[0]["right"] <= boxes[1]["left"] + 1
        mode = page.locator(".agent-mode").bounding_box()
        meta = page.locator(".chat-meta").bounding_box()
        assert mode["y"] + mode["height"] <= meta["y"] + 1
        view = page.locator("#view").bounding_box()
        assert mode["y"] >= view["y"] - 2 and mode["y"] + mode["height"] <= view["y"] + view["height"] + 2
        page.locator("#view").evaluate("e => {e.scrollTop=e.scrollHeight}")
        sticky = page.locator(".agent-mode").bounding_box()
        assert sticky["y"] >= view["y"] - 2 and sticky["y"] + sticky["height"] <= view["y"] + view["height"]
        screenshot_if_requested(page, f"{language}-320-agent-5-1")
        assert page.locator(".composer button[aria-label]").count() >= 2  # attachment and send

        page.locator("#gear").click()
        page.locator(".mode-killswitch").wait_for(state="visible")
        assert page.evaluate("window.__sentUpdates.filter(x => ['kill_switch','nauta_strict'].includes(x.payload?.op)).length") == 0
        page.get_by_text("Administración" if language == "es" else "Administration", exact=True).first.click()
        holder = page.locator(".maintenance-settings")
        holder.wait_for(state="visible")
        assert not holder.locator('input[type="checkbox"]').is_checked()
        assert page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'maintenance_notice').length") == 0
        holder.locator('input[type="checkbox"]').check()
        holder.get_by_label("Duración en minutos" if language == "es" else "Duration in minutes").fill("300")
        assert page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'maintenance_notice').length") == 0
        holder.get_by_role("button", name="Guardar aviso" if language == "es" else "Save notice").click()
        page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'maintenance_notice' && x.payload.args.minutes === 300 && x.payload.args.active === true)")
        screenshot_if_requested(page, f"{language}-maintenance-5-1")
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_weather_and_real_history_charts_are_data_driven(language):
    with browser_fixture(language, {"width": 375, "height": 844}) as (page, errors):
        page.evaluate("go('weather')")
        page.locator("#view input").fill("Havana")
        page.locator("#view .search-field button").click()
        page.locator(".weather-hero").wait_for(state="visible")
        assert "28" in page.locator(".weather-temp").inner_text()
        assert page.locator(".weather-day").count() == 2
        assert page.locator(".weather-hero svg").count() >= 4
        assert page.locator(".weather-result select").count() == 1
        page.locator(".weather-result select").select_option("2")
        page.wait_for_function("window.__sentUpdates.some(x => x.payload?.op === 'weather' && x.payload.args.place_id === 2)")
        screenshot_if_requested(page, f"{language}-weather-5-1")
        page.evaluate("go('rates')")
        page.get_by_role("button", name="Consultar historial" if language == "es" else "Load history").click()
        page.locator(".rate-chart").wait_for(state="visible")
        assert page.locator(".chart-bar").count() == 7
        assert page.locator(".rate-chart").evaluate("e => e.scrollWidth <= e.clientWidth")
        assert page.locator(".scenario").count() == 0  # provider permission not assumed
        assert ("pendientes" if language == "es" else "await") in page.locator(".rate-chart").locator("..").inner_text()
        assert page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'rates' && x.payload.args.history).length") == 1
        page.locator(".rate-history-table").scroll_into_view_if_needed()
        screenshot_if_requested(page, f"{language}-rates-5-1")
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_nested_model_and_image_selection_with_legacy_history_preserved(language):
    with browser_fixture(language, {"width": 390, "height": 844}) as (page, errors):
        page.evaluate("""() => {
          cache.models = {reset: Date.now()/1000+86400, pin:'', providers:[{key:'test', name:'Test Provider', daily:100, used:2, cooldown:0, personal:false,
            models:[{key:'test:alpha',title:'Alpha',id:'alpha',roles:['chat','reason'],daily:10,used:1,cooldown:0,accepts:['document','image']},
              {key:'test:beta',title:'Beta',id:'beta',roles:['chat','code'],daily:10,used:1,cooldown:0,accepts:['document']}] }],
            images:[{key:'demo:flux',label:'FLUX Paint',daily:10,used:1,cooldown:0,personal:false},{key:'demo:sdxl',label:'SDXL Paint',daily:10,used:0,cooldown:0,personal:false}] };
          store.set('chat.auto',[{role:'user',text:'legacy preserved',ts:1}]); saveCache(); goTab('ai');
        }""")
        assert page.get_by_text("Conversación automática anterior" if language == "es" else "Previous automatic conversation").count() == 1
        assert page.locator(".provider-card:not([open])").count() == 1
        page.locator(".provider-card > summary").click()
        assert page.locator(".provider-card .model-category").count() == 2
        assert page.get_by_text("1 modelo" if language == "es" else "1 model", exact=True).count() == 2
        page.locator(".provider-card .model-category").first.locator("summary").click()
        assert page.locator(".provider-card .model-choice:visible").count() >= 1
        page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
        page.get_by_role("tab", name="Con IA / API" if language == "es" else "With AI / API").click()
        page.locator(".agent-picker > summary").click()
        page.locator(".agent-provider > summary").click()
        page.locator(".agent-provider .model-category").first.locator("summary").click()
        page.get_by_role("button", name="Alpha", exact=False).first.click()
        assert page.evaluate("prefs.agentModel") == "test:alpha"
        assert page.evaluate("store.get('chat.auto')[0].text") == "legacy preserved"
        page.evaluate("go('images')")
        page.locator(".image-provider > summary").click()
        assert page.locator(".image-provider .image-family").count() == 2
        assert page.get_by_text("1 modelo" if language == "es" else "1 model", exact=True).count() == 2
        page.locator(".image-family").filter(has_text="Stable Diffusion").locator("summary").click()
        page.get_by_role("button", name="SDXL Paint", exact=False).click()
        assert page.evaluate("prefs.imageModel") == "demo:sdxl"
        assert page.evaluate("store.get('chat.auto')[0].text") == "legacy preserved"
        screenshot_if_requested(page, f"{language}-models-5-1")
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_failed_model_fallback_uses_unified_agent_without_resetting_selection(language):
    with browser_fixture(language, {"width": 320, "height": 720}) as (page, errors):
        page.evaluate("""() => {
          prefs.agentModel = 'test:chosen';
          saveMessages('smart_agent', [
            {role:'user',text:'Retry this task',ts:101},
            {role:'assistant',text:'',error:'SIMULATED failure',ts:102}
          ]);
          go('chat', {space:'smart_agent'});
        }""")
        page.get_by_role("button", name="Usar Agente con IA (automático)" if language == "es" else "Use AI Agent (automatic)").click()
        request = page.evaluate("window.__sentUpdates.map(x => x.payload).filter(x => x && x.op === 'ask').at(-1).args")
        assert request["space"] == "smart_agent"
        assert "agent_model" not in request
        assert page.evaluate("prefs.agentModel") == "test:chosen"
        assert page.evaluate("(store.get('chat.auto', []) || []).length") == 0
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_retry_never_silently_drops_lost_attachment(language):
    with browser_fixture(language, {"width": 320, "height": 720}) as (page, errors):
        page.evaluate("""() => {
          saveMessages('smart_agent', [
            {role:'user',text:'Please read',ts:201,file:{name:'example.txt',size:10,kind:'document'}},
            {role:'assistant',text:'',error:'SIMULATED failure',ts:202}
          ]);
          go('chat', {space:'smart_agent'});
        }""")
        page.get_by_role("button", name="Usar Agente con IA (automático)" if language == "es" else "Use AI Agent (automatic)").click()
        page.get_by_text("El adjunto ya no está en memoria. Vuelve a adjuntarlo para reintentar." if language == "es" else
                         "The attachment is no longer in memory. Attach it again to retry.").wait_for()
        assert page.evaluate("window.__sentUpdates.filter(x => x.payload && x.payload.op === 'ask').length") == 0
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_eltoque_crypto_units_and_unconfirmed_codes_are_never_converted(language):
    # Synthetic explicit opt-in exercises unit restrictions, not provider approval.
    with browser_fixture(language, {"width": 320, "height": 800}) as (page, errors):
        page.evaluate("""() => {
          const rates = {USD:855, BTC:780.65, USDT_TRC20:1095, TRX:424.69, ECU:950};
          rateDerivedApproved = true; // simulate a fresh policy response from this bot session
          store.set('rates',{rates, at:Date.now()/1000, source:'elTOQUE API', derived_allowed:true});
          store.set('rates.history',{history:Array.from({length:7},(_,i)=>({
            date:new Date(Date.UTC(2026,9,1+i)).toISOString().slice(0,10), rates
          })), complete:true, source:'elTOQUE API', days:7, derived_allowed:true});
          go('rates');
        }""")
        cards = page.locator(".rate-grid")
        assert ("1 USD en BTC" if language == "es" else "1 USD in BTC") in cards.inner_text()
        assert ("USDT_TRC20 · unidad por confirmar" if language == "es" else "USDT_TRC20 · unit unconfirmed") in cards.inner_text()
        assert ("unidad por confirmar" if language == "es" else "unit unconfirmed") in cards.inner_text()
        options = page.locator(".convert-row select option").all_text_contents()
        assert "USD" in options and "TRX" in options
        assert not {"BTC", "USDT_TRC20", "ECU"}.intersection(options)
        assert page.locator("#view select option[value='30']").count() == 1
        history = page.locator(".rate-chart").locator("..")
        history.locator("select").select_option("BTC")
        scenario = history.locator(".scenario").inner_text()
        assert ("1 USD en BTC" if language == "es" else "1 USD in BTC") in scenario
        history.locator("select").select_option("ECU")
        assert history.locator(".scenario strong").count() == 0
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        screenshot_if_requested(page, f"{language}-eltoque-units")
        assert errors == []


@pytest.mark.parametrize("language,width", [("es", 320), ("en", 375)])
def test_home_rates_banner_stale_and_provider_policy_fail_closed(language, width):
    with browser_fixture(language, {"width": width, "height": 800}) as (page, errors):
        # Cache contains an obsolete approval from an earlier session. A hello
        # without current approval must never re-enable derived calculations.
        page.evaluate("""() => {
          const rates = {USD:855, ECU:950, MLC:727.23, BTC:780.65};
          rateDerivedApproved = false;
          store.set('rates',{rates, at:Date.now()/1000, source:'elTOQUE API', refresh_seconds:300, derived_allowed:true});
          store.set('rates.history',{history:Array.from({length:7},(_,i)=>({
            date:new Date(Date.UTC(2026,9,1+i)).toISOString().slice(0,10), rates
          })),complete:true,source:'elTOQUE API',days:7,derived_allowed:true});
          go('home');
        }""")
        banner = page.locator(".rate-banner")
        assert "elTOQUE" in banner.inner_text() and "855" in banner.inner_text()
        assert "[object" not in banner.inner_text() and banner.locator("[data-since]").count() == 1
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        screenshot_if_requested(page, f"{language}-rates-banner-fresh")
        banner.click()
        assert page.locator(".rates-summary").is_visible()
        assert "[object" not in page.locator(".rates-summary").inner_text()
        assert page.locator(".convert-row input").is_disabled()
        assert page.locator(".convert-out").inner_text() == ""
        assert page.locator(".scenario").count() == 0
        page.locator(".rate-history-table summary").click()
        assert page.locator(".history-row").count() == 8
        assert "855" in page.locator(".history-rows").inner_text()
        page.locator(".rate-about summary").click()
        assert "https://eltoque.com/" in page.locator(".rate-about .rate-source-url").inner_text()
        page.evaluate("""() => {
          const item = store.get('rates'); store.set('rates',{...item, stale:true}); bus.emit('rates');
        }""")
        assert page.locator(".rate-status.stale").is_visible()
        assert ("guardad" if language == "es" else "saved") in page.locator("#view .note.warn").first.inner_text().lower()
        page.evaluate("go('home')")
        assert page.locator(".rate-banner.is-stale").is_visible()
        assert ("guardad" if language == "es" else "saved") in page.locator(".rate-banner").inner_text().lower()
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        screenshot_if_requested(page, f"{language}-rates-banner-stale")
        assert errors == []


@pytest.mark.parametrize("language", ["es", "en"])
def test_single_guest_mobile_uses_real_access_handlers(language, monkeypatch):
    from michiflin import access, admin, config, miniapp, state
    access.reset()
    monkeypatch.setattr(config, "OWNER_ADDRESS", "owner@example.org")
    monkeypatch.setattr(config, "OWNER_ONLY", True)
    monkeypatch.setattr(config, "MAINTENANCE_MODE", False)
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    monkeypatch.setattr(admin, "_set_blocked_contact", lambda *_: True)
    monkeypatch.setattr(miniapp.perf, "snapshot", lambda: None)
    monkeypatch.setattr(miniapp.usage, "report", lambda: None)
    monkeypatch.setitem(state.data, "access", {"allowed": ["historical@example.org"], "blocked": [], "attempts": {}, "alerts": True})
    try:
        with browser_fixture(language, {"width": 320, "height": 800}, real_access=True) as (page, errors):
            page.evaluate("go('admin')")
            card = page.locator(".single-guest")
            card.wait_for(state="visible")
            card.locator("input").fill("guest@example.org")
            card.locator("select").select_option("60")
            assert not admin.is_allowed("guest@example.org")
            card.locator("button").click()
            assert not admin.is_allowed("guest@example.org")  # confirmation is deliberate
            page.locator("#sheet .btn.danger").click()
            page.get_by_text("guest@example.org", exact=True).wait_for(state="visible")
            assert admin.is_allowed("guest@example.org") and not admin.is_allowed("historical@example.org")
            assert access.snapshot()["guests"] == ["guest@example.org"]
            assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
            screenshot_if_requested(page, f"{language}-single-guest")
            assert errors == []
    finally:
        access.reset()
