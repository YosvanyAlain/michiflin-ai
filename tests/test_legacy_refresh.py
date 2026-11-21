"""Original-identity refresh: real browser rendering, synthetic mail only."""
from __future__ import annotations

import pytest

from michiflin import miniapp
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine,width", [("chromium", 320), ("webkit", 390), ("chromium", 1280)])
def test_saved_rates_and_upcoming_forecast_use_banner_space_without_requests(engine, width):
    with browser_fixture("es", {"width": width, "height": 844}, engine=engine) as (page, errors):
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("""() => {
          const at=now()-300;
          store.set('rates.observations',[{at:at-300,value:320}]);
          store.set('rates',{at,refresh_seconds:300,rates:{USD:325,EUR:350,MLC:270,CAD:230,MXN:17,TRX:80,BTC:0.001}});
          const dates=Array.from({length:6},(_,i)=>new Date(Date.now()+(i-1)*86400000).toISOString().slice(0,10));
          store.set('weatherHome',{city:'Prueba',interval:21600,last:{at:now(),data:{
            timezone:'UTC',place:{label:'Prueba'},current:{code:0,temp:28,is_day:1},
            days:dates.map(date=>({date,code:0,high:30,low:23}))}}});
          goTab('home');
        }""")
        page.wait_for_function("viewReady()")
        before = page.evaluate("window.__sentUpdates.length")
        search_y = page.locator(".searchbox").bounding_box()["y"]
        page.locator(".home-banner-dot").nth(1).click()
        assert abs(page.locator(".searchbox").bounding_box()["y"] - search_y) <= 2
        assert page.locator(".rate-banner-values > span").count() == 6
        assert page.locator(".rate-banner-values").get_by_text("CAD", exact=True).is_visible()
        assert not page.locator(".rate-banner-values").get_by_text("BTC", exact=True).count()
        assert page.locator(".rate-sparkline polyline").get_attribute("points").count(" ") == 1
        assert page.evaluate("store.get('rates.observations',[]).length") == 2
        page.locator(".home-banner-dot").nth(2).click()
        assert abs(page.locator(".searchbox").bounding_box()["y"] - search_y) <= 2
        assert page.locator(".forecast-day").count() == 4
        assert all(day > page.evaluate("new Date().toISOString().slice(0,10)")
                   for day in page.locator(".forecast-day time").evaluate_all("nodes=>nodes.map(n=>n.dateTime)"))
        for selector in [".weather-banner", ".forecast-days", "#view"]:
            assert page.locator(selector).evaluate("n=>n.scrollWidth<=n.clientWidth+1")
        assert page.evaluate("window.__sentUpdates.length") == before
        assert errors == []


def test_chart_never_invents_observations_and_forecast_never_relabels_past_days():
    with browser_fixture("en", {"width": 390, "height": 844}) as (page, errors):
        page.evaluate("""() => {
          store.del('rates.observations');
          saveRateObservation({at:now()-100,rates:{USD:320}});
          saveRateObservation({at:now()-50,stale:true,rates:{USD:350}});
          saveRateObservation({at:now()+1000,rates:{USD:400}});
          saveRateObservation({at:now()-200,rates:{USD:310}});
        }""")
        assert page.evaluate("store.get('rates.observations',[]).length") == 1
        assert page.evaluate("store.get('rates.observations',[])[0].value") == 320
        assert page.evaluate("rateObservationChart({stale:true}).querySelector('polyline')") is None
        assert page.evaluate("homeForecast({timezone:'UTC',days:[{date:'2020-01-01'},{date:'2099-02-30'}]}).querySelectorAll('.forecast-day').length") == 0
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_original_teal_and_retired_style_fallback(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.evaluate("prefs.style='pixel';prefs.theme='light';prefs.accent='style';applyLook()")
        assert page.evaluate("prefs.style") == "michiflin"
        assert page.evaluate("VISUAL_STYLES.length") == 13
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()") == "#0e8577"
        assert page.locator(".mascot-seated").count() == 1
        page.evaluate("prefs.theme='dark';applyLook()")
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()") == "#2bb5a3"
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_splash_cat_stops_at_yarn_then_continues_and_does_not_wait_for_mail(engine):
    with browser_fixture("en", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.add_init_script("Object.defineProperty(window,'__delay',{configurable:true,get(){return 60000},set(_v){}})")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("!!document.querySelector('.mascot-running')")
        positions = page.evaluate("""() => {
          const node=document.querySelector('.splash-runner'), a=node.getAnimations()[0];
          a.pause();
          return [100,850,1350,2150].map(ms=>{a.currentTime=ms;return node.getBoundingClientRect().left;});
        }""")
        assert positions[0] < positions[1]
        assert abs(positions[1] - positions[2]) < 1
        assert positions[3] > positions[2] + 100
        page.locator("#splash").wait_for(state="detached", timeout=3500)
        assert page.evaluate("Object.values(bridge.pending()).some(v=>v.op==='hello')")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_missed_download_push_recovers_notice_from_sync_and_deduplicates(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.evaluate("""() => {
          goTab('apps');
          const job={id:'907',title:'Prueba YouTube',kind:'video',status:'downloading',created:10,updated:20};
          setJobs([job]);window.__jobs=[{...job,status:'done',updated:30}];
        }""")
        page.evaluate("hello(true,true)")
        badge = page.locator('.tab[data-tab="downloads"]:visible .tab-badge')
        badge.wait_for(state="visible")
        assert badge.inner_text() == "1"
        page.evaluate("bridge.pushes.job(window.__jobs[0]);setJobs(window.__jobs)")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.job?.id==='907').length") == 1
        # An older, delayed status cannot roll the queue backward.
        page.evaluate("bridge.pushes.job({...window.__jobs[0],status:'downloading',updated:25})")
        assert page.evaluate("cache.jobs.find(j=>j.id==='907').status") == "done"
        page.reload()
        page.wait_for_function("cache.at>0")
        assert page.evaluate("store.get('notices',[]).filter(n=>n.job?.id==='907').length") == 1
        assert errors == []


@pytest.mark.parametrize("status", ["ready", "done", "failed", "cancelled"])
def test_each_download_outcome_notifies_without_waiting_for_another_tab(status):
    with browser_fixture("en", {"width": 390, "height": 844}) as (page, errors):
        page.evaluate("goTab('apps')")
        page.evaluate("status=>bridge.pushes.job({id:'991',title:'Outcome check',kind:'video',created:10,updated:20,status})", status)
        assert page.locator('.tab[data-tab="downloads"]:visible .tab-badge').inner_text() == "1"
        assert page.evaluate("store.get('notices',[]).find(n=>n.job?.id==='991').job.status") == status
        assert errors == []


def test_job_handoff_failure_is_retryable_and_ready_has_notification(monkeypatch):
    monkeypatch.setattr(miniapp.maintenance, "silent_active", lambda: False)
    monkeypatch.setattr(miniapp, "_job_marks", {})
    monkeypatch.setattr(miniapp, "job_view", lambda job, _locale: dict(job))
    calls = []
    responses = iter([False, True])

    def push(*args):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(miniapp, "push", push)
    job = {"id": "991", "title": "Prueba", "chat": 12, "status": "ready", "locale": "es"}
    miniapp._on_job(job, {"status": "ready"})
    assert not miniapp._job_marks
    miniapp._on_job(job, {"status": "ready"})
    assert "Lista para enviar" in calls[-1][-1]
    miniapp._on_job(job, {"status": "ready"})
    assert len(calls) == 2


def test_exact_pre_refresh_package_remains_epoch_bound(monkeypatch, tmp_path):
    import shutil
    from michiflin import app_compat, config

    monkeypatch.setattr(config, "BOT_EPOCH", "test-epoch")
    old = app_compat.pre_refresh_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert old == "55cacec817"
    assert miniapp.compatible_build({"build": old})
    assert miniapp.discloses_infobars({"build": old})
    monkeypatch.setattr(config, "BOT_EPOCH", "different-epoch")
    assert not miniapp.compatible_build({"build": old})
    monkeypatch.setattr(config, "BOT_EPOCH", "test-epoch")
    archived = tmp_path / "archive"
    shutil.copytree(app_compat._PRE_REFRESH, archived)
    monkeypatch.setattr(app_compat, "_PRE_REFRESH", archived)
    with (archived / "app.js").open("a") as stream:
        stream.write("\n// synthetic alteration\n")
    assert app_compat.pre_refresh_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == ""
    assert not miniapp.compatible_build({"build": old})
