"""Premium UI and local notification tests; all replies are synthetic."""
from __future__ import annotations

import copy
import json
from unittest import mock

import pytest

from michiflin import app_compat, config, live_web, miniapp, state
from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


@pytest.mark.parametrize("engine,width", [("chromium", 390), ("webkit", 320)])
def test_model_overview_counts_local_limits_without_double_counting(engine, width):
    with browser_fixture("es", {"width": width, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.evaluate("""() => {
          cache.models = {now: Date.now()/1000, reset: Date.now()/1000+5000, images: [
            {key:'image:a', label:'Dibujo', daily:30, used:4},
            {key:'personal-one:image', label:'Dibujo personal', personal:true, daily:10, used:2, attempts:9}], providers: [
            {key:'shared', name:'Compartido', shared_limit:true, daily:10, used:3, models:[
              {key:'shared:a', title:'Beam', id:'reflection/beam', daily:10, used:2, local_available:true, manual:true, roles:['chat']},
              {key:'shared:b', title:'Pausado', id:'test', daily:10, used:1, local_available:false, roles:['chat']}]},
            {key:'separate', name:'Separado', shared_limit:false, daily:20, used:12, models:[
              {key:'separate:a', title:'Flint', id:'recursive/flint', daily:10, used:12, local_available:false, roles:['chat']},
              {key:'separate:b', title:'Laguna', id:'poolside/laguna', daily:10, used:0, local_available:true, roles:['chat']}]},
            {key:'personal-one', name:'Personal', personal:true, daily:0, used:2, models:[
              {key:'personal-one:x', title:'Unknown sample', id:'custom/model', daily:0, used:0, local_available:true, roles:['chat']}]}
          ]}; bus.emit('models');
        }""")
        page.locator('.tab[data-tab="ai"]:visible').click()
        board = page.locator(".model-overview")
        assert board.locator(".overview-stat.used strong").inner_text() == "17"
        assert board.locator(".overview-stat.left strong").inner_text() == "17"  # 7 shared + 10 other
        assert board.locator(".overview-stat.active strong").inner_text() == "3"
        assert board.locator(".overview-stat.paused strong").inner_text() == "2"
        board.locator("summary").click()
        assert "2 registrados · límite desconocido" in board.inner_text()
        assert "4 registrados · 26 restantes de 30 locales" in board.inner_text()
        assert "2 imágenes registradas · 9 intentos compartidos · 1 restantes de 10 locales" in board.inner_text()
        assert "no saldo ni facturación" in board.inner_text()
        page.locator(".provider-card summary").first.click()
        page.locator(".model-category summary").first.click()
        assert page.locator(".model-choice .brand-monogram").count() >= 1
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        screenshot_if_requested(page, "premium-stats-mobile")
        assert errors == []


def test_desktop_premium_overview_respects_light_and_dark_without_overflow():
    with browser_fixture("en", {"width": 1280, "height": 820}) as (page, errors):
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.evaluate("() => { prefs.theme='dark'; applyLook(); }")
        assert page.locator(".model-overview").is_visible()
        screenshot_if_requested(page, "premium-desktop-dark")
        page.evaluate("() => { prefs.theme='light'; applyLook(); }")
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ai_pending_and_unread_badges_open_actual_chat_and_clear(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=1200")
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.get_by_text("Agente", exact=True).first.click()
        page.locator("#view textarea").fill("prueba sintética")
        page.locator("#view .composer button.primary").click()
        page.locator('.tab[data-tab="apps"]:visible').click()
        ai_tab = page.locator('.tab[data-tab="ai"]:visible')
        assert "1 en curso" in ai_tab.get_attribute("aria-label")
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread && n.target?.view==='chat')")
        assert "1 sin leer" in ai_tab.get_attribute("aria-label")
        page.locator('.tab[data-tab="home"]:visible').click()
        notice = page.locator("button.notice").filter(has_text="Agente").first
        notice.click()
        assert page.evaluate("current.view") == "chat"
        assert "SIMULATED agent answer" in page.locator("#view").inner_text()
        assert ai_tab.locator(".tab-badge").is_hidden()
        assert errors == []


@pytest.mark.parametrize("target", ["youtube", "results"])
def test_search_survives_navigation_and_lost_waiter_without_repeating(target):
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => { window.__delay=1000; window.__searchResults = [
          {title:'Resultado sintético', url:'https://youtube.com/watch?v=synthetic', snippet:'Información de prueba'}]; }""")
        if target == "youtube":
            page.evaluate("() => go('youtube', {q:'consulta de prueba'})")
            op, section, notice_text = "youtube_search", "apps", "YouTube está lista"
        else:
            page.evaluate("() => go('results', {q:'consulta de prueba',engine:'web',mode:'standard'})")
            op, section, notice_text = "search", "web", "consulta web está lista"
        page.wait_for_function("op => window.__sentUpdates.some(x=>x.payload?.op===op)", arg=op)
        # Models a closed/reopened miniapp: the callback is gone but persisted request context remains.
        page.evaluate("""op => { const m = Object.entries(bridge.pending()).find(([,v])=>v.op===op); bridge.waiting.delete(m[0]); }""", op)
        page.locator('.tab[data-tab="home"]:visible').click()
        tab = page.locator(f'.tab[data-tab="{section}"]:visible')
        assert "1 en curso" in tab.get_attribute("aria-label")
        page.wait_for_function("name => store.get('notices',[]).some(x=>x.unread && x.text.includes(name))", arg=notice_text)
        assert "1 sin leer" in tab.get_attribute("aria-label")
        page.locator("button.notice").filter(has_text=notice_text).first.click()
        page.get_by_text("Resultado sintético", exact=True).first.wait_for(state="visible")
        assert page.evaluate("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        assert tab.locator(".tab-badge").is_hidden()
        assert errors == []


def test_expired_search_notice_does_not_silently_repeat_the_request():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=700")
        page.evaluate("() => go('results', {q:'no repetir',engine:'web',mode:'standard'})")
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).some(n=>n.target?.view==='results')")
        page.evaluate("() => { memo.clear(); store.del(RESULT_CACHE_KEY); }")
        page.locator("button.notice").filter(has_text="consulta web está lista").click()
        page.get_by_text("El resultado guardado ya no está disponible", exact=False).wait_for(state="visible")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='search').length") == 1
        assert page.evaluate("current.view") == "home"
        assert errors == []


def test_disclosure_marker_survives_restart_retries_only_failed_hand_off(monkeypatch, tmp_path):
    from michiflin import collab_agent, local_ai

    data = copy.deepcopy(state.data)
    data["apps"] = {"10": {"epoch": "fixture"}, "11": {"epoch": "fixture"}}
    monkeypatch.setattr(state, "data", data)
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "BOT_EPOCH", "fixture")
    monkeypatch.setattr(local_ai, "configured", lambda: True)
    monkeypatch.setattr(miniapp.maintenance, "silent_active", lambda: False)
    monkeypatch.setattr(collab_agent, "notice_for_instance", lambda _id: False)
    send = mock.Mock(return_value=True)
    monkeypatch.setattr(miniapp, "_send_status", send)
    miniapp._local_ai_disclosed.clear()
    miniapp._local_ai_disclosure(10)
    assert send.call_count == 1 and state.data["apps"]["10"]["local_ai_disclosure"] == "local-ai-1"
    # Restore the state file and lose the process-local cache.
    monkeypatch.setattr(state, "data", json.loads((tmp_path / "state.json").read_text()))
    miniapp._local_ai_disclosed.clear()
    miniapp._local_ai_disclosure(10)
    assert send.call_count == 1
    send.return_value = False
    miniapp._local_ai_disclosure(11)
    assert "local_ai_disclosure" not in state.data["apps"]["11"]
    send.return_value = True
    miniapp._local_ai_disclosure(11)
    assert send.call_count == 3
    assert state.data["apps"]["11"]["local_ai_disclosure"] == "local-ai-1"
    # A genuinely different disclosure is delivered once, not to all instances.
    monkeypatch.setattr(collab_agent, "notice_for_instance", lambda _id: True)
    miniapp._local_ai_disclosure(10)
    miniapp._local_ai_disclosure(10)
    assert send.call_count == 4
    assert state.data["apps"]["10"]["local_ai_disclosure"] == "collaboration-1"
    miniapp._local_ai_disclosed.clear()


def test_both_exact_legacy_packages_and_infobar_disclosure(monkeypatch, tmp_path):
    import shutil

    epoch = "premium-test"
    monkeypatch.setattr(config, "BOT_EPOCH", epoch)
    older = app_compat.previous_build(epoch, str(miniapp.APP_DIR), miniapp.APP_FILES)
    recent = app_compat.recent_build(epoch, str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert older and recent and older != recent and recent != miniapp.app_build()
    assert app_compat.recent_build("test-epoch", str(miniapp.APP_DIR), miniapp.APP_FILES) == "35e3d9314d"
    assert miniapp.compatible_build({"build": older})
    assert miniapp.compatible_build({"build": recent})
    assert not miniapp.discloses_infobars({"build": older})
    assert miniapp.discloses_infobars({"build": recent})
    session = live_web.Session(10, "a" * 32, "owner@example.invalid", "host:example.org", "https://example.org/", browser="human")
    with mock.patch.object(live_web, "_authorized", return_value=({"build": recent}, "owner@example.invalid")):
        assert session._hide_infobars()
    old_dir = tmp_path / "recent"
    shutil.copytree(app_compat._RECENT, old_dir)
    monkeypatch.setattr(app_compat, "_RECENT", old_dir)
    with (old_dir / "app.js").open("a") as f:
        f.write("tampered")
    assert not app_compat.recent_build(epoch, str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert not miniapp.compatible_build({"build": recent})
    assert not miniapp.discloses_infobars({"build": recent})

@pytest.mark.parametrize("feature", ["translate", "weather", "tools"])
def test_other_tool_results_are_visible_without_silent_requery(feature):
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=900")
        page.evaluate("name => go(name)", feature)
        if feature == "translate":
            page.locator("#view textarea").fill("Hola mundo")
            page.get_by_role("button", name="Traducir", exact=True).click()
            op, notice_text, expected = "translate", "Traductor", "translated smoke test"
        elif feature == "weather":
            page.locator("#view input").first.fill("Havana")
            page.get_by_role("button", name="Consultar").first.click()
            op, notice_text, expected = "weather", "Clima", "Havana, Cuba"
        else:
            page.locator(".utility-card summary").first.click()
            page.locator(".utility-card input").first.fill("1 EUR en CUP")
            page.locator(".utility-card button").first.click()
            op, notice_text, expected = "tool", "Utilidades", "smoke test result"
        page.wait_for_function("op => window.__sentUpdates.some(x=>x.payload?.op===op)", arg=op)
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("name => store.get('notices',[]).some(n=>n.unread && n.text.includes(name))", arg=notice_text)
        page.locator("button.notice").filter(has_text=notice_text).first.click()
        result = page.locator(".weather-place").get_by_text(expected, exact=False) if feature == "weather" else page.get_by_text(expected, exact=False).first
        result.wait_for(state="visible")
        assert page.evaluate("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        assert errors == []


def test_new_app_discards_duplicate_disclosure_after_notice_is_cleared():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.evaluate("""() => {
          const send = () => bridge.receive({serial: ++window.__serial, payload: {t:'push',kind:'notice',
            data:{code:'collaboration-1',kind:'info',es:'Aviso de seguridad sintético',en:'Synthetic safety notice'}}});
          send(); store.set('notices',[]); bus.emit('notices'); send();
        }""")
        assert page.evaluate("store.get('noticeCodes',[]).includes('collaboration-1')")
        assert page.evaluate("store.get('notices',[]).length") == 0
        assert errors == []


def test_recent_58a_package_requests_exactly_one_new_app_only_after_confirmation(tmp_path):
    """Synthetic handler and WebKit; no real bot/account or production messages."""
    import time
    from concurrent.futures import Future
    from contextlib import ExitStack, nullcontext

    from michiflin import access, admin, chat, maintenance, perf

    class Immediate:
        def submit(self, function, *args):
            future = Future()
            try:
                future.set_result(function(*args))
            except Exception as error:
                future.set_exception(error)
            return future

    owner, epoch = "owner@example.org", "fixture-epoch"
    prior = app_compat.recent_build(epoch, str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert prior
    entry = {"chat": 7, "addr": owner, "build": prior, "epoch": epoch,
             "locale": "es", "sent": time.time(), "serial": 0, "access_session": ""}
    unchanged = copy.deepcopy(entry)
    synthetic_state = copy.deepcopy(state.data)
    synthetic_state["apps"] = {"100": entry}
    sent, replies = [], []

    def fake_send(chat_id, text, **kwargs):
        assert chat_id == 7 and kwargs.get("viewtype") == "Webxdc"
        assert not kwargs.get("interrupted", lambda: False)()
        sent.append((text, kwargs))
        return 201

    def record_response(instance, request_id, data, error="", **_kwargs):
        assert instance == 100
        replies.append((request_id, data, error))

    def delivery_handler(request):
        assert request.get("op") == "reinstall"
        before = len(replies)
        miniapp.handle(100, dict(entry), request)
        if len(replies) != before + 1 or replies[-1][2]:
            raise ValueError("synthetic delivery refused")
        return replies[-1][1]

    with ExitStack() as stack:
        for obj, name, value in (
            (state, "data", synthetic_state), (state, "save", lambda: None),
            (state, "maintenance_active", lambda: False), (state, "maintenance_interrupted", lambda _generation: False),
            (state, "get_kill_switch", lambda _address: False), (state, "kill_generation", lambda _address: 0),
            (state, "work_interrupted", lambda _address, _generation: False), (state, "guard_work", lambda *_args: nullcontext()),
            (config, "BOT_EPOCH", epoch), (config, "BOT_EPOCH_STRICT", True), (config, "TEMP_ROOT", tmp_path),
            (config, "service_access_allowed", lambda address: address == owner),
            (access, "bound_allowed", lambda item: item.get("addr") == owner),
            (admin, "is_allowed", lambda address: address == owner), (admin, "seen", lambda _address: None),
            (miniapp, "_verified_binding", lambda _entry: True), (miniapp, "_workers", Immediate()),
            (miniapp, "_upgraded", {}), (miniapp, "respond", record_response),
            (live_web, "_identity", lambda instance: (entry, owner) if instance == 100 else None),
            (chat, "sender", lambda: owner), (chat, "send", fake_send),
            (maintenance, "text", lambda _locale: ""), (perf, "note_op", lambda *_args: None),
        ):
            stack.enter_context(mock.patch.object(obj, name, value))
        with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True,
                             legacy_package="recent", delivery_handler=delivery_handler) as (page, errors):
            assert sent == []
            page.evaluate("go('settings')")
            request = page.get_by_role("button", name="Pedir app nueva", exact=True)
            request.tap()
            page.get_by_text("Cada instancia tiene almacenamiento independiente", exact=False).wait_for()
            assert sent == []
            page.get_by_role("button", name="Cancelar", exact=True).tap()
            assert sent == []
            request.tap()
            page.get_by_role("button", name="Enviar app nueva", exact=True).tap()
            page.get_by_text("Listo: abre la app nueva", exact=False).wait_for()
            assert len(sent) == 1
            assert set(synthetic_state["apps"]) == {"100", "201"}
            assert synthetic_state["apps"]["100"] == unchanged
            assert synthetic_state["apps"]["201"]["build"] == miniapp.app_build()
            request.tap()
            page.get_by_role("button", name="Enviar app nueva", exact=True).tap()
            page.wait_for_function("window.__fixtureResponses.filter(op => op === 'reinstall').length === 2")
            assert len(sent) == 1 and replies[-1][1].get("already")
            assert errors == []


def test_concurrent_disclosure_handoff_preserves_changed_revision(monkeypatch, tmp_path):
    """Concurrent hellos do not duplicate a revision or overwrite a newer one."""
    import threading
    from michiflin import collab_agent, local_ai

    data = copy.deepcopy(state.data)
    data["apps"] = {"10": {"epoch": "fixture"}}
    monkeypatch.setattr(state, "data", data)
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "BOT_EPOCH", "fixture")
    monkeypatch.setattr(local_ai, "configured", lambda: True)
    monkeypatch.setattr(miniapp.maintenance, "silent_active", lambda: False)
    collaboration = threading.Event()
    monkeypatch.setattr(collab_agent, "notice_for_instance", lambda _id: collaboration.is_set())
    entered, release = threading.Event(), threading.Event()
    codes, errors = [], []

    def send(_instance, payload):
        codes.append(payload["data"]["code"])
        if len(codes) == 1:
            entered.set()
            assert release.wait(4)
        return True

    def call():
        try:
            miniapp._local_ai_disclosure(10)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(miniapp, "_send_status", send)
    miniapp._local_ai_disclosed.clear()
    first = threading.Thread(target=call, daemon=True)
    rest = [threading.Thread(target=call, daemon=True) for _ in range(7)]
    try:
        first.start()
        assert entered.wait(3)
        collaboration.set()
        for worker in rest:
            worker.start()
    finally:
        release.set()
        first.join(4)
        for worker in rest:
            if worker.ident is not None:
                worker.join(4)
    assert not errors and not first.is_alive() and all(not worker.is_alive() for worker in rest)
    assert codes == ["local-ai-1", "collaboration-1"]
    assert state.data["apps"]["10"]["local_ai_disclosure"] == "collaboration-1"
    assert json.loads((tmp_path / "state.json").read_text())["apps"]["10"]["local_ai_disclosure"] == "collaboration-1"
    miniapp._local_ai_disclosed.clear()


def test_personal_image_overview_uses_shared_attempt_budget(monkeypatch):
    from michiflin import ai, images, personal_ai
    source = images.Source("personal-synthetic:picture", "Synthetic image", 10, personal=True)
    monkeypatch.setattr(ai, "providers", lambda: [])
    monkeypatch.setattr(images, "sources", lambda: [source])
    monkeypatch.setattr(personal_ai, "attempts", lambda provider: 9 if provider == "personal-synthetic" else 0)
    monkeypatch.setattr(state, "usage_today", lambda key: 2 if key == source.key else 0)
    data = miniapp.models_view()["images"][0]
    assert data["used"] == 2 and data["attempts"] == 9 and data["daily"] == 10


@pytest.mark.parametrize("feature", ["youtube", "translate", "weather", "tools"])
def test_badged_shortcut_returns_to_pending_result_without_an_extra_request(feature):
    with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => { window.__delay=1500; window.__searchResults = [
            {title:'Resultado sintético',url:'https://youtube.com/watch?v=synthetic',snippet:'prueba'}]; }""")
        if feature == "youtube":
            page.evaluate("() => go('youtube', {q:'consulta pendiente'})")
            op, expected = "youtube_search", "Resultado sintético"
        else:
            page.evaluate("name => go(name)", feature)
            if feature == "translate":
                page.locator("#view textarea").fill("Hola")
                page.get_by_role("button", name="Traducir", exact=True).tap()
                op, expected = "translate", "translated smoke test"
            elif feature == "weather":
                page.locator("#view input").first.fill("Havana")
                page.get_by_role("button", name="Consultar", exact=True).first.tap()
                op, expected = "weather", "Havana, Cuba"
            else:
                page.locator(".utility-card summary").first.tap()
                page.locator(".utility-card input").first.fill("1 km en m")
                page.locator(".utility-card button").first.tap()
                op, expected = "tool", "smoke test result"
        page.wait_for_function("op => window.__sentUpdates.some(x=>x.payload?.op===op)", arg=op)
        page.locator('.tab[data-tab="home"]:visible').tap()
        shortcut = page.locator(f".tile-{feature}").first
        assert "1 en curso" in shortcut.get_attribute("aria-label")
        shortcut.tap()
        assert page.evaluate("current.view") == feature
        if feature == "translate":
            assert page.get_by_role("button", name="Traducir", exact=True).is_disabled()
        result = page.locator(".weather-place").get_by_text(expected, exact=False) if feature == "weather" else page.get_by_text(expected, exact=False).first
        result.wait_for(state="visible")
        assert page.evaluate("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        assert not page.evaluate("feature => store.get('notices',[]).some(n=>n.unread && n.target?.view===feature)", feature)
        assert errors == []


def test_two_unread_utility_results_are_individually_locatable():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=1400; go('tools')")
        convert = page.locator(".utility-card").nth(0)
        convert.locator("summary").click()
        convert.locator("input").fill("1 km en m")
        convert.locator("button").click()
        world_time = page.locator(".utility-card").nth(1)
        world_time.locator("summary").click()
        world_time.locator("input").fill("La Habana")
        world_time.locator("button").click()
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("store.get('notices',[]).filter(n=>n.unread && n.target?.view==='tools').length===2")
        page.locator("button.notice").filter(has_text="Conversor de unidades").click()
        assert page.evaluate("current.params.tool") == "convert"
        assert page.locator(".utility-card").nth(0).locator(".utility-body").get_by_text("smoke test result").is_visible()
        page.locator('.tab[data-tab="home"]:visible').click()
        page.locator("button.notice").filter(has_text="Hora en el mundo").click()
        assert page.evaluate("current.params.tool") == "time"
        assert page.locator(".utility-card").nth(1).locator(".utility-body").get_by_text("smoke test result").is_visible()
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='tool').length") == 2
        assert errors == []


@pytest.mark.parametrize("feature", ["youtube", "results", "tools"])
def test_failed_background_request_shows_error_without_hidden_retry(feature):
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => { window.__delay=800; window.__searchResults = [
          {title:'Resultado tras reintento',url:'https://youtube.com/watch?v=synthetic',snippet:'prueba'}]; }""")
        op = {"youtube": "youtube_search", "results": "search", "tools": "tool"}[feature]
        page.evaluate("op => window.__failNext[op]=1", op)
        if feature == "youtube":
            page.evaluate("() => go('youtube', {q:'fallo sintético'})")
        elif feature == "results":
            page.evaluate("() => go('results', {q:'fallo sintético',engine:'web',mode:'standard'})")
        else:
            page.evaluate("go('tools')")
            page.locator(".utility-card summary").first.click()
            page.locator(".utility-card input").first.fill("1 km en m")
            page.locator(".utility-card button").first.click()
        page.wait_for_function("op => window.__sentUpdates.some(x=>x.payload?.op===op)", arg=op)
        page.locator('.tab[data-tab="home"]:visible').click()
        page.wait_for_function("feature => store.get('notices',[]).some(n=>n.unread && n.kind==='bad' && n.target?.view===feature)", arg=feature)
        page.locator("button.notice.bad").filter(has_text="terminó con error").click()
        assert page.evaluate("current.view") == feature
        page.get_by_text("SIMULATED network uncertainty", exact=False).first.wait_for(state="visible")
        assert page.evaluate("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        page.get_by_role("button", name="Reintentar", exact=True).first.click()
        page.wait_for_function("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length===2", arg=op)
        assert errors == []


def test_six_result_cache_evicts_oldest_even_with_identical_timestamps():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        result = page.evaluate("""() => {
          const realNow=Date.now, fixed=realNow(); Date.now=()=>fixed;
          try { for (let i=0;i<7;i++) saveResult('synthetic-'+i, {n:i}); }
          finally { Date.now=realNow; }
          return Object.keys(store.get(RESULT_CACHE_KEY, {}));
        }""")
        assert len(result) == 6 and "synthetic-6" in result and "synthetic-0" not in result
        assert errors == []


def test_expired_badged_shortcut_warns_instead_of_repeating():
    with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=700; go('youtube',{q:'no repetir'})")
        page.locator('.tab[data-tab="home"]:visible').tap()
        page.wait_for_function("store.get('notices',[]).some(n=>n.unread && n.target?.view==='youtube')")
        page.evaluate("""() => {
          const rec=store.get(RESULT_CACHE_KEY,{});
          for (const [key,value] of Object.entries(rec)) { value.at=now()-RESULT_CACHE_SECONDS-1; resultMemoTimes.set(key,value.at); }
          store.set(RESULT_CACHE_KEY,rec);
        }""")
        page.locator(".tile-youtube").first.tap()
        page.get_by_text("El resultado guardado ya no está disponible", exact=False).wait_for(state="visible")
        assert page.evaluate("current.view") == "home"
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length") == 1
        assert errors == []


@pytest.mark.parametrize("feature", ["coder", "images"])
def test_coder_and_images_have_navigable_pending_and_unread_cards(feature):
    with browser_fixture("es", {"width": 390, "height": 844}, engine="webkit", touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("window.__delay=1400")
        page.evaluate("name => go(name)", feature)
        page.locator("#view textarea").first.fill("Contenido sintético")
        page.get_by_role("button", name="Programar" if feature == "coder" else "Generar", exact=True).tap()
        op, label = ("code", "Programador") if feature == "coder" else ("image", "Imágenes IA")
        page.wait_for_function("op => window.__sentUpdates.some(x=>x.payload?.op===op)", arg=op)
        page.locator('.tab[data-tab="ai"]:visible').tap()
        card = page.locator("button.space-card").filter(has_text=label).first
        assert "1 en curso" in card.get_attribute("aria-label")
        page.wait_for_function("feature => store.get('notices',[]).some(n=>n.unread && n.target?.view===feature)", arg=feature)
        assert "1 sin leer" in card.get_attribute("aria-label")
        card.tap()
        assert page.evaluate("current.view") == feature
        if feature == "coder":
            page.get_by_text("synthetic.txt", exact=True).wait_for(state="visible")
        else:
            page.locator('img[alt="Contenido sintético"]').wait_for(state="visible")
        assert page.evaluate("op => window.__sentUpdates.filter(x=>x.payload?.op===op).length", op) == 1
        assert not page.evaluate("feature => store.get('notices',[]).some(n=>n.unread && n.target?.view===feature)", feature)
        assert errors == []


def test_agent_shortcut_does_not_open_an_unread_different_model_chat():
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate("""() => {
          notice('Modelo sintético listo','ok',{category:'ai',target:{view:'chat',params:{space:'vendor:other'}}});
          goTab('home');
        }""")
        tile = page.locator(".tile-ai").first
        assert tile.locator(".tile-badge").is_hidden()
        tile.click()
        assert page.evaluate("current.params.space") == "agent"
        assert page.evaluate("store.get('notices',[]).some(n=>n.unread && n.target?.params?.space==='vendor:other')")
        assert errors == []


def test_legacy_hash_is_not_admitted_under_a_different_epoch(monkeypatch):
    monkeypatch.setattr(config, "BOT_EPOCH", "first-epoch")
    old = app_compat.previous_build(config.BOT_EPOCH, str(miniapp.APP_DIR), miniapp.APP_FILES)
    recent = app_compat.recent_build(config.BOT_EPOCH, str(miniapp.APP_DIR), miniapp.APP_FILES)
    assert old and recent and miniapp.compatible_build({"build": old}) and miniapp.compatible_build({"build": recent})
    monkeypatch.setattr(config, "BOT_EPOCH", "different-epoch")
    assert not miniapp.compatible_build({"build": old})
    assert not miniapp.compatible_build({"build": recent})
    assert not miniapp.discloses_infobars({"build": recent})


def test_disclosure_save_failure_retries_only_after_restart(monkeypatch):
    from michiflin import collab_agent, local_ai
    data = copy.deepcopy(state.data)
    data["apps"] = {"10": {"epoch": "fixture"}}
    monkeypatch.setattr(state, "data", data)
    monkeypatch.setattr(config, "BOT_EPOCH", "fixture")
    monkeypatch.setattr(local_ai, "configured", lambda: True)
    monkeypatch.setattr(miniapp.maintenance, "silent_active", lambda: False)
    monkeypatch.setattr(collab_agent, "notice_for_instance", lambda _id: True)
    send = mock.Mock(return_value=True)
    monkeypatch.setattr(miniapp, "_send_status", send)
    monkeypatch.setattr(state, "save", mock.Mock(side_effect=OSError("simulated disk failure")))
    miniapp._local_ai_disclosed.clear()
    miniapp._local_ai_disclosure(10)
    assert "local_ai_disclosure" not in data["apps"]["10"]
    miniapp._local_ai_disclosure(10)
    assert send.call_count == 1
    miniapp._local_ai_disclosed.clear()
    monkeypatch.setattr(state, "save", lambda: None)
    miniapp._local_ai_disclosure(10)
    assert send.call_count == 2 and data["apps"]["10"]["local_ai_disclosure"] == "collaboration-1"
    miniapp._local_ai_disclosed.clear()
