"""Follow-up UI acceptance. Synthetic views only, no device identifiers."""
from unittest.mock import Mock
import pytest
from michiflin import access, admin, chat, config, miniapp, state
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,width', [('chromium',320),('chromium',360),('chromium',393),('chromium',412),('webkit',320),('webkit',390),('webkit',768),('chromium',1440)])
@pytest.mark.parametrize('locale', ['es','en'])
def test_sources_and_download_tabs_have_full_labels(engine,width,locale):
    with browser_fixture(locale,{'width':width,'height':844},engine=engine,touch=width<800) as (page,errors):
        for size in [100,130,160]:
            page.evaluate('n=>document.documentElement.style.fontSize=n+"%"',size)
            page.evaluate("go('web')")
            sources=page.locator('.search-selectors .segmented').last
            assert sources.locator('button').count()==3
            for control in sources.locator('button').all():
                assert control.evaluate('e=>e.scrollWidth<=e.clientWidth+1&&e.scrollHeight<=e.clientHeight+1')
                assert control.bounding_box()['height']>=44
            page.evaluate("go('downloads')")
            assert page.locator('.download-tabs button').count()==3
            for control in page.locator('.download-tabs button').all():
                assert control.evaluate('e=>e.scrollWidth<=e.clientWidth+1&&e.scrollHeight<=e.clientHeight+1')
                box=control.bounding_box();assert box['x']>=0 and box['x']+box['width']<=width+1
            assert page.locator('#view').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
        assert errors==[]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('locale', ['es', 'en'])
def test_narrow_titles_wrap_without_clipping_or_overlapping_controls(engine, locale):
    with browser_fixture(locale, {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        for size in (100, 160):
            page.evaluate('n=>document.documentElement.style.fontSize=n+"%"', size)
            for view in ('official_connections', 'help', 'weather', 'personal_ai', 'downloads', 'admin', 'settings', 'tools'):
                page.evaluate('view=>go(view)', view)
                title = page.locator('#title')
                assert title.evaluate('e=>e.scrollWidth<=e.clientWidth+1 && e.scrollHeight<=e.clientHeight+1')
                bounds = title.bounding_box()
                assert bounds['x'] >= page.locator('#back').bounding_box()['x'] + page.locator('#back').bounding_box()['width'] - 1
                assert bounds['x'] + bounds['width'] <= page.locator('#notifications-toggle').bounding_box()['x'] + 1
                assert page.locator('.top').bounding_box()['height'] <= 105
        assert errors == []


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_bell_is_the_only_dot_and_animates_until_no_unread(engine):
    with browser_fixture('es',{'width':390,'height':844},engine=engine) as (page,errors):
        page.emulate_media(reduced_motion='no-preference')
        page.evaluate("notice('Respuesta','ok',{category:'ai',target:{view:'chat',params:{space:'agent'}}});notice('General')")
        bell=page.locator('#notifications-toggle')
        assert bell.locator('.notification-count').inner_text()=='2'
        assert bell.locator('svg').evaluate('e=>getComputedStyle(e).animationName')=='bell-unread'
        assert page.locator('.tab-badge:visible,.tile-badge:visible,.space-badge:visible').count()==0
        page.evaluate("goTab('ai')")
        assert page.locator('.space-badge:visible').count()==0
        page.evaluate("goTab('apps')")
        assert page.locator('.tile-badge:visible').count()==0
        page.emulate_media(reduced_motion='reduce')
        assert bell.locator('svg').evaluate('e=>getComputedStyle(e).animationName')=='none'
        page.evaluate("store.set('notices',[]);bus.emit('notices')")
        assert not bell.evaluate('e=>e.classList.contains("has-unread")')
        assert bell.locator('.notification-count').is_hidden()
        assert errors==[]


@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('theme,system',[('dark','light'),('dark','dark'),('light','dark'),('light','light')])
def test_startup_is_opaque_and_brief_without_waiting_for_transport(engine,theme,system):
    script=f"localStorage.setItem('mf.prefs',JSON.stringify({{theme:'{theme}'}}))"
    with browser_fixture('es',{'width':393,'height':851},engine=engine,init_script=script) as (page,errors):
        page.emulate_media(color_scheme=system)
        held=[]
        page.route('**/webxdc.js',lambda route:held.append(route))
        page.goto(page.url,wait_until='commit')
        mark=page.locator('#boot-mark');mark.wait_for(state='visible')
        assert page.locator('html').get_attribute('data-boot-theme')==theme
        assert mark.evaluate('e=>getComputedStyle(e).backgroundColor')==('rgb(18, 28, 36)' if theme=='dark' else 'rgb(250, 249, 246)')
        assert mark.locator('svg').bounding_box()['width']>40
        assert page.evaluate('document.querySelector("#app").inert')
        assert len(held)==1
        page.wait_for_timeout(120)
        assert mark.is_visible()  # transport has not even been evaluated
        page.evaluate('window.__michiflinPaintStart=Date.now()')
        held[0].continue_()
        page.wait_for_function('window.__michiflinBootReady')
        page.wait_for_timeout(150)
        assert mark.is_visible()
        page.wait_for_function('window.__michiflinStartupDone',timeout=2500)
        assert page.evaluate('Date.now()-window.__michiflinPaintStart')>=800
        assert not page.evaluate('document.querySelector("#app").inert')
        assert page.locator('#welcome-guidance').count()==0
        assert errors==[]


def test_new_weather_ui_has_no_implied_region():
    with browser_fixture('en',{'width':360,'height':800}) as (page,errors):
        page.evaluate("store.del('weatherHome');store.del('cities');go('weather')")
        assert page.locator('#view input').first.input_value()==''
        assert page.get_by_role('button',name='Enable refresh',exact=True).is_disabled()
        assert page.evaluate("!window.__sentUpdates.some(x=>x.payload?.op==='weather')")
        assert errors==[]


def test_current_only_revokes_old_build_without_deleting_any_instance(monkeypatch):
    monkeypatch.setattr(config,'MINIAPP_CURRENT_ONLY',True)
    monkeypatch.setattr(config,'BOT_EPOCH','synthetic')
    monkeypatch.setattr(miniapp,'app_build',lambda:'current')
    apps={'11':{'epoch':'synthetic','build':'old','chat':42,'addr':'owner@example.invalid','history':'keep'},
          '12':{'epoch':'synthetic','build':'current','chat':42,'addr':'owner@example.invalid'}}
    monkeypatch.setitem(state.data,'apps',apps)
    assert miniapp._instance(11) is None
    assert miniapp._instance(12) is apps['12']
    assert not miniapp.compatible_build({'epoch':'foreign','build':'current'})
    monkeypatch.setattr(access,'bound_allowed',lambda entry:True)
    monkeypatch.setattr(admin,'is_allowed',lambda address:True)
    monkeypatch.setattr(miniapp,'_verified_binding',lambda entry:True)
    monkeypatch.setattr(state,'save',Mock())
    send=Mock(return_value=True);monkeypatch.setattr(chat,'send_update',send)
    assert miniapp._notify_retired_instance(11)
    assert not miniapp._notify_retired_instance(11)
    assert apps['11']['history']=='keep' and len(apps)==2
    assert send.call_count==1
    assert '/app' in send.call_args.args[1]['data']['es']
    monkeypatch.setattr(miniapp,'_verified_binding',lambda entry:False)
    apps['11'].pop('retired_notice')
    assert not miniapp._notify_retired_instance(11)
    assert send.call_count==1


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_sheet_opened_during_startup_keeps_modal_lock_and_unlocks_after_close(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        held = []
        page.route('**/webxdc.js', lambda route: held.append(route))
        page.goto(page.url, wait_until='commit')
        page.locator('#boot-mark').wait_for(state='visible')
        page.evaluate('window.__michiflinPaintStart = Date.now()')
        assert len(held) == 1
        held[0].continue_()
        page.wait_for_function('window.__michiflinBootReady && !window.__michiflinStartupDone')
        page.evaluate("openSheet('Startup sheet', 'Synthetic content')")
        page.wait_for_function('window.__michiflinStartupDone')
        assert page.locator('#app').evaluate('el => el.inert')
        assert page.get_by_role('dialog', name='Startup sheet').is_visible()
        page.evaluate('closeSheet()')
        page.locator('#sheet').wait_for(state='hidden')
        assert not page.locator('#app').evaluate('el => el.inert')
        assert errors == []


def test_startup_watchdog_recovers_if_host_script_hangs_then_resumes():
    with browser_fixture('en', {'width': 390, 'height': 844}) as (page, errors):
        page.evaluate("localStorage.setItem('mf.kept', 'synthetic project')")
        # Accelerate only the watchdog's eight-second delay in this fixture.
        page.add_init_script("""(() => {const timer=window.setTimeout;
          window.setTimeout=(fn,delay,...args)=>timer(fn,delay===8000?350:delay,...args);})()""")
        held = []
        page.route('**/webxdc.js', lambda route: held.append(route))
        page.goto(page.url, wait_until='commit')
        page.locator('#startup-recovery:not([hidden])').wait_for(timeout=3000)
        assert page.locator('#boot-mark').count() == 0
        assert '/app' in page.locator('#startup-recovery').inner_text()
        assert page.evaluate("localStorage.getItem('mf.kept')") == 'synthetic project'
        assert len(held) == 1
        held[0].continue_()
        page.wait_for_function('window.__michiflinStartupDone', timeout=5000)
        assert page.locator('#startup-recovery').is_hidden()
        assert not page.locator('#app').evaluate('el => el.inert')
        assert page.evaluate("localStorage.getItem('mf.kept')") == 'synthetic project'
        assert errors == []


def test_current_only_gates_replies_even_when_legacy_flag_is_off(monkeypatch):
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'REVOKE_LEGACY_MINIAPPS', False)
    for name in ['BOT_EPOCH_STRICT', 'BOT_VERIFIED_CONTACTS_ONLY', 'EMAIL_KEY_RECOVERY', 'INVITE_AUTO_ACCESS']:
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic')
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current')
    old = {'epoch': 'synthetic', 'build': 'old', 'chat': 42, 'addr': 'owner@example.invalid'}
    monkeypatch.setitem(state.data, 'apps', {'11': old})
    monkeypatch.setattr(access, 'bound_allowed', lambda entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda address: True)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    send = Mock(); monkeypatch.setattr(chat, 'send_update', send)
    admit = Mock(side_effect=AssertionError('a retired request must not be admitted'))
    monkeypatch.setattr(miniapp, '_admit', admit)
    assert not miniapp._release_allows(11, old)
    assert not miniapp.discloses_infobars(old)
    assert not miniapp._send_status(11, {'t': 'res'})
    miniapp._route(11, old, {'id': 'retired', 'op': 'hello'})
    miniapp.respond(11, 'retired', {'unexpected': True})
    admit.assert_not_called()
    send.assert_not_called()
    assert state.data['apps']['11'] is old


def test_retired_notice_fails_closed_and_reserves_before_sending(monkeypatch):
    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic')
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current')
    old = {'epoch': 'synthetic', 'build': 'old', 'chat': 42,
           'addr': 'owner@example.invalid', 'history': 'keep'}
    monkeypatch.setitem(state.data, 'apps', {'11': old})
    monkeypatch.setattr(access, 'bound_allowed', lambda entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda address: True)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    send = Mock(return_value=False); monkeypatch.setattr(chat, 'send_update', send)
    monkeypatch.setattr(state, 'save', Mock(side_effect=OSError('disk full')))
    assert not miniapp._notify_retired_instance(11)
    assert 'retired_notice' not in old
    send.assert_not_called()
    monkeypatch.setattr(state, 'save', Mock())
    assert not miniapp._notify_retired_instance(11)  # synthetic handoff failure
    assert old['retired_notice'] == 'current'       # persisted before handoff
    assert send.call_count == 1
    assert not miniapp._notify_retired_instance(11)  # no duplicate after failure
    assert send.call_count == 1 and old['history'] == 'keep'


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('locale', ['es', 'en'])
def test_first_home_banner_greets_for_device_time_in_both_languages(engine, locale):
    with browser_fixture(locale, {'width': 320, 'height': 640}, engine=engine) as (page, errors):
        page.emulate_media(reduced_motion='reduce')
        expected = (['Buenas noches', 'Buenas noches', 'Buenos días', 'Buenos días',
                     'Buenas tardes', 'Buenas tardes', 'Buenas noches', 'Buenas noches']
                    if locale == 'es' else
                    ['Good evening', 'Good evening', 'Good morning', 'Good morning',
                     'Good afternoon', 'Good afternoon', 'Good evening', 'Good evening'])
        assert page.evaluate('[0,4,5,11,12,18,19,23].map(hour => greeting(hour))') == expected
        first = page.locator('.home-banner-controls button').first
        assert first.get_attribute('aria-label') == ('Saludo' if locale == 'es' else 'Greeting')
        assert first.get_attribute('aria-current') == 'true'
        assert page.locator('.home-banner-slot .home-hero h2').inner_text() == page.evaluate('greeting()')
        assert page.locator('.home-banner-slot .rate-banner,.home-banner-slot .weather-banner').count() == 0
        # Even an in-progress task keeps the greeting as the first banner.
        page.evaluate("cache.jobs.push({id:'synthetic',status:'downloading'}); bus.emit('jobs')")
        assert page.locator('.home-banner-slot .home-hero h2').inner_text() == page.evaluate('greeting()')
        assert ('tareas en curso' if locale == 'es' else 'tasks in progress') in page.locator('.home-hero p').inner_text()
        assert errors == []


@pytest.mark.parametrize('engine,locale', [('chromium', 'es'), ('webkit', 'en')])
def test_manual_guide_reflows_at_narrow_width_and_large_text(engine, locale):
    with browser_fixture(locale, {'width': 320, 'height': 568}, engine=engine, touch=True) as (page, errors):
        page.evaluate("document.documentElement.style.fontSize='160%';go('help')")
        page.get_by_role('button', name='Ver guía rápida' if locale == 'es' else 'Open quick guide').click()
        # The sheet slides in; assess its settled layout, not its opening frame.
        page.wait_for_function("""() => {const panel=document.querySelector('#sheet .sheet-panel');
          const r=panel.getBoundingClientRect();return getComputedStyle(panel).transform==='none'
            && r.top>=0 && r.bottom<=innerHeight+1;}""")
        for step in range(5):
            panel = page.locator('#sheet .sheet-panel')
            assert panel.evaluate('e => {const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1}')
            assert page.locator('.quick-tour').evaluate('e => e.scrollWidth<=e.clientWidth+1'), step
            assert page.locator('.quick-tour h3').inner_text().strip()
            page.locator('.quick-tour .btn.primary').click()
        page.locator('#sheet').wait_for(state='hidden')
        assert errors == []


def test_sweep_does_not_broadcast_retirement_notices(monkeypatch):
    import threading
    from michiflin import maintenance

    monkeypatch.setattr(config, 'MINIAPP_CURRENT_ONLY', True)
    monkeypatch.setattr(config, 'BOT_EPOCH', 'synthetic')
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'current')
    old = {'epoch': 'synthetic', 'build': 'old', 'chat': 42,
           'addr': 'owner@example.invalid', 'serial': 7, 'history': 'keep'}
    monkeypatch.setitem(state.data, 'apps', {'11': old})
    monkeypatch.setattr(miniapp, '_paused', threading.Event())
    monkeypatch.setattr(access, 'bound_allowed', lambda entry: True)
    monkeypatch.setattr(admin, 'is_allowed', lambda address: True)
    monkeypatch.setattr(miniapp, '_verified_binding', lambda entry: True)
    save = Mock(); monkeypatch.setattr(state, 'save', save)
    send = Mock(return_value=True); monkeypatch.setattr(chat, 'send_update', send)
    rpc = Mock(); monkeypatch.setattr(chat, 'rpc', lambda: rpc)
    monkeypatch.setattr(maintenance, 'get', lambda: {'revision': 0})
    monkeypatch.setattr(miniapp, '_prune_seen', lambda: None)
    monkeypatch.setattr(miniapp.time, 'sleep', Mock(side_effect=StopIteration))
    with pytest.raises(StopIteration):
        miniapp.sweep_forever()
    save.assert_not_called()
    send.assert_not_called()
    rpc.get_webxdc_status_updates.assert_not_called()
    assert old['serial'] == 7 and old['history'] == 'keep'
    # Only actual new status activity may get one best-effort recovery hint.
    miniapp.on_status_update(11, from_event=True)
    assert send.call_count == 1 and old['retired_notice'] == 'current'
    rpc.get_webxdc_status_updates.assert_not_called()
    monkeypatch.setattr(miniapp, 'app_build', lambda: 'later-build')
    miniapp.on_status_update(11, from_event=True)
    assert send.call_count == 1 and old['history'] == 'keep'


def test_core_event_dispatch_marks_real_activity_for_retirement_hint(monkeypatch):
    import threading
    from types import SimpleNamespace
    from michiflin import app

    monkeypatch.setattr(app, '_draining', threading.Event())
    monkeypatch.setattr(app, '_app_events_pending', {})
    monkeypatch.setattr(app, '_app_event_slots', threading.BoundedSemaphore(1))
    monkeypatch.setattr(app, '_app_event_pool', SimpleNamespace(submit=lambda fn: fn()))
    monkeypatch.setattr(chat, 'ready', lambda: True)
    monkeypatch.setattr(miniapp, '_instance', lambda _msg_id: None)
    dispatch = Mock(); monkeypatch.setattr(miniapp, 'on_status_update', dispatch)
    app.on_app_update(None, 1, SimpleNamespace(msg_id=11))
    dispatch.assert_called_once_with(11, from_event=True)
