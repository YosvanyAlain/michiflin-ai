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
        assert page.locator(".scenario strong").count() == 1
        assert "elTOQUE" in page.locator(".scenario").inner_text()
        assert page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'rates' && x.payload.args.history).length") == 1
        page.locator(".scenario").scroll_into_view_if_needed()
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
        page.locator(".image-family").filter(has_text="Stable Diffusion").locator("summary").click()
        page.get_by_role("button", name="SDXL Paint", exact=False).click()
        assert page.evaluate("prefs.imageModel") == "demo:sdxl"
        assert page.evaluate("store.get('chat.auto')[0].text") == "legacy preserved"
        screenshot_if_requested(page, f"{language}-models-5-1")
        assert errors == []
