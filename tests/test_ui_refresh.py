"""Owner-requested simpler UI. Synthetic responses, never live production I/O."""
from pathlib import Path
from unittest.mock import Mock
import hashlib
import json
import os
import tempfile

import pytest

from michiflin import app_compat, chat, config, miniapp, state
from tests.test_webxdc_browser import browser_fixture

SCREENSHOTS = Path(os.getenv("MICHIFLIN_UI_SCREENSHOTS", str(Path(tempfile.gettempdir()) / "michiflin-ui-screenshots")))


def test_removed_feature_not_packaged_or_dispatched():
    assert 'social' not in miniapp.OPS and 'social' not in miniapp.WORK_OPS
    assert 'social.js' not in miniapp.APP_FILES
    assert not (miniapp.APP_DIR / 'social.js').exists()
    assert not (miniapp.APP_DIR.parent / 'michiflin/text_social.py').exists()
    assert 'tile_social' not in (miniapp.APP_DIR / 'app.js').read_text()


def test_guidance_permanent_preference_is_per_verified_sender_and_survives_new_app(monkeypatch):
    monkeypatch.setitem(state.data, 'ui_guidance', {})
    saved = Mock(); monkeypatch.setattr(state, 'save', saved)
    monkeypatch.setattr(chat, 'sender', lambda: 'owner@example.invalid')
    assert miniapp.op_guidance(40, {}, 'es') == {'dismissed': False}
    assert miniapp.op_guidance(40, {'dismiss': True}, 'es') == {'dismissed': True}
    # A fresh instance/chat has the same person, not the old webview storage.
    assert miniapp.op_guidance(90, {}, 'es') == {'dismissed': True}
    saved.assert_called_once()
    monkeypatch.setattr(chat, 'sender', lambda: 'other@example.invalid')
    assert miniapp.op_guidance(50, {}, 'en') == {'dismissed': False}
    assert miniapp.op_guidance(50, {'dismiss': 'true'}, 'en') == {'dismissed': False}
    monkeypatch.setattr(chat, 'sender', lambda: '')
    with pytest.raises(miniapp.AppError):
        miniapp.op_guidance(50, {'dismiss': True}, 'en')


def test_package_hash_cached_but_invalidated_after_source_change(monkeypatch, tmp_path):
    monkeypatch.setattr(miniapp, 'APP_DIR', tmp_path)
    monkeypatch.setattr(miniapp, 'APP_FILES', ('x.js',))
    file = tmp_path / 'x.js'; file.write_text('first')
    miniapp._cached_app_build.cache_clear()
    first = miniapp.app_build()
    hits = miniapp._cached_app_build.cache_info().hits
    assert miniapp.app_build() == first
    assert miniapp._cached_app_build.cache_info().hits == hits + 1
    file.write_text('second changed')
    assert miniapp.app_build() != first
    monkeypatch.setattr(config, 'BOT_EPOCH', 'another-epoch')
    assert miniapp.app_build() != first


def test_previously_shipped_ui_never_bypasses_epoch_or_release():
    registry = json.loads((Path(app_compat.__file__).parent / 'compat/ui-release-builds.json').read_text())
    for build in registry['builds']:
        assert not app_compat.shipped_ui_build('foreign', registry['release'], build)
    assert hashlib.sha256(b'foreign').hexdigest() != registry['epoch_sha256']


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_background_rates_are_quiet_manual_refresh_is_not(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("() => { window.__delay=60000; window.backgroundRate=refreshSharedRates().catch(()=>{}); }")
        page.wait_for_function("Object.values(bridge.pending()).some(x=>x.op==='rates')")
        assert page.evaluate("Object.values(bridge.pending()).filter(x=>x.op==='rates').every(x=>x.quiet)")
        assert page.locator('#activity').is_hidden()
        sent_before = page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='rates').length")
        page.evaluate('() => {refreshSharedRates(false).catch(()=>{});}')
        assert page.locator('#activity').is_visible()
        assert page.evaluate("Object.values(bridge.pending()).filter(x=>x.op==='rates').every(x=>!x.quiet)")
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='rates').length") == sent_before
        page.evaluate("() => { const id=Object.keys(bridge.pending()).find(id=>bridge.pending()[id].op==='rates'); bridge.forget(id); }")
        page.wait_for_function('sharedRateRefresh === null')
        page.evaluate('() => {refreshSharedRates(false).catch(()=>{});}')
        page.wait_for_function("Object.values(bridge.pending()).some(x=>x.op==='rates' && !x.quiet)")
        assert page.locator('#activity').is_visible()
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_books_are_idle_on_entry_and_query_all_catalogues(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("go('books')")
        page.get_by_placeholder('Título, autor o tema').wait_for()
        assert page.locator('#view select').input_value() == 'all'
        page.wait_for_timeout(250)
        assert page.evaluate("!window.__sentUpdates.some(x=>x.payload?.op==='books')")
        page.get_by_placeholder('Título, autor o tema').fill('El principito')
        page.get_by_role('button', name='Buscar', exact=True).click()
        page.wait_for_function("window.__sentUpdates.some(x=>x.payload?.op==='books' && x.payload.args.q==='El principito' && x.payload.args.source==='all')")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_youtube_toggle_does_not_repeat_search_or_download(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine) as (page, errors):
        page.evaluate("""() => {store.set('youtube.showThumbnails',false);window.__searchResults=[{title:'Vídeo sintético',url:'https://www.youtube.com/watch?v=abcdefghijk'}];go('youtube',{q:'fixture'});}""")
        page.get_by_text('Vídeo sintético', exact=True).wait_for()
        assert page.locator('.video-preview').is_hidden()
        assert page.evaluate("!window.__sentUpdates.some(x=>x.payload?.op==='youtube_thumbs')")
        before = page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length")
        page.get_by_role('button', name='Opciones de YouTube', exact=True).click()
        page.get_by_label('Mostrar miniaturas').check()
        assert page.locator('.video-preview').is_visible()
        page.get_by_label('Mostrar miniaturas').uncheck()
        assert page.locator('.video-preview').is_hidden()
        assert page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='youtube_search').length") == before
        assert page.evaluate("!window.__sentUpdates.some(x=>x.payload?.op==='action')")
        assert errors == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_compact_notices_hidden_preview_swipe_and_global_delete(engine):
    with browser_fixture('es', {'width': 390, 'height': 844}, engine=engine, touch=True) as (page, errors):
        page.evaluate("""() => {notice('Visible');notice('Oculta');const ns=store.get('notices',[]);ns[0].hidden=true;store.set('notices',ns);}""")
        page.locator('#notifications-toggle').click()
        assert page.locator('.notice-center-row').count() == 1
        row = page.locator('.notice-center-row')
        row.dispatch_event('pointerdown', {'clientX': 260, 'clientY': 150, 'isPrimary': True})
        row.dispatch_event('pointerup', {'clientX': 150, 'clientY': 151, 'isPrimary': True})
        assert row.get_attribute('data-swipe') == 'delete'
        assert page.evaluate("store.get('notices',[]).length") == 2
        page.get_by_role('button', name='Ocultas', exact=True).click()
        assert page.locator('.notice-center-row').count() == 1
        assert page.locator('.notice-center-row').inner_text().startswith('Oculta')
        assert page.evaluate("store.get('notices',[]).find(x=>x.text==='Oculta').hidden")
        page.get_by_role('button', name='Recientes', exact=True).click()
        page.get_by_role('button', name='Eliminar todas', exact=True).click()
        # New arrivals during a destructive confirmation are not part of it.
        page.evaluate("notice('Llegó después de confirmar la selección')")
        page.locator('#sheet').get_by_role('button', name='Eliminar', exact=True).click()
        page.wait_for_function("store.get('notices',[]).length===1")
        assert page.evaluate("store.get('notices',[])[0].text") == 'Llegó después de confirmar la selección'
        assert errors == []


@pytest.mark.parametrize('engine,locale', [('chromium','es'), ('webkit','en')])
def test_nonblocking_welcome_can_skip_or_permanently_dismiss(engine, locale):
    captured = []
    def handler(request):
        captured.append(request)
        assert request['op'] == 'guidance' and request['args']['dismiss'] is True
        return {'dismissed': True}
    with browser_fixture(locale, {'width': 390, 'height': 844}, engine=engine, delivery_handler=handler) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate('welcomeGuidance(false)')
        assert page.locator('#welcome-guidance').is_visible()
        assert page.evaluate("!document.querySelector('#app').inert")
        page.locator('#welcome-guidance').get_by_role('button', name='No volver a mostrar' if locale == 'es' else 'Never show again').click()
        page.locator('#welcome-guidance').wait_for(state='detached')
        assert len(captured) == 1
        assert page.evaluate("store.get('tour_permanent', false)")
        page.evaluate("() => {store.del('tour_permanent');store.del('tour_done');welcomeGuidance(true);}")
        assert page.locator('#welcome-guidance').count() == 0
        page.evaluate('welcomeGuidance(false)')
        page.locator('#welcome-guidance').get_by_role('button', name='Saltar' if locale == 'es' else 'Skip', exact=True).click()
        assert page.locator('#welcome-guidance').count() == 0
        assert errors == []


MATRIX = [('chromium',320,568,'es','light'), ('webkit',390,844,'es','dark'),
          ('chromium',412,915,'en','dark'), ('webkit',768,1024,'en','light'),
          ('chromium',1024,768,'es','light'), ('webkit',1440,900,'en','dark')]


@pytest.mark.parametrize('engine,width,height,locale,theme', MATRIX)
def test_changed_screens_responsive_matrix(engine, width, height, locale, theme):
    target = SCREENSHOTS / f'{engine}-{width}-{height}-{locale}-{theme}'
    target.mkdir(parents=True, exist_ok=True)
    with browser_fixture(locale, {'width':width,'height':height}, engine=engine, touch=width<800) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("value=>{prefs.theme=value;applyLook()}", theme)
        screens = [('home',{}), ('ai',{}), ('chat',{'space':'agent'}), ('chat',{'space':'auto'}),
                   ('web',{}), ('apps',{}), ('youtube',{}), ('books',{}), ('rates',{}), ('downloads',{}), ('settings',{})]
        screens += [('settings',{'section':section}) for section in ['appearance','ai','navigation','bot','privacy','library','connection','support']]
        for index, (view, params) in enumerate(screens):
            page.evaluate('([view,params])=>go(view,params)', [view,params])
            page.wait_for_timeout(80)
            assert page.locator('#view').evaluate('e=>e.scrollWidth<=e.clientWidth+1'), (view,params)
            assert page.locator('#view').inner_text().strip(), (view,params)
            assert page.locator('#view .failure').count() == 0, (view,params)
            if view == 'chat':
                assert page.locator('#view .intro .chip').count() == 0
            page.screenshot(path=str(target / f'{index:02d}-{view}.png'))
        page.evaluate("notice('Notificación de prueba')")
        page.locator('#notifications-toggle').click()
        page.wait_for_function("() => {const r=document.querySelector('#sheet .sheet-panel').getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight+1}")
        page.screenshot(path=str(target/'20-notificaciones.png'))
        assert page.locator('#sheet .sheet-panel').evaluate('e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1}')
        assert errors == []


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_startup_has_immediate_lightweight_mark_without_waiting_for_bot(theme):
    with browser_fixture('es', {'width':360,'height':800}) as (page, errors):
        page.emulate_media(color_scheme=theme, reduced_motion='reduce')
        held = []
        page.route('**/app.js', lambda route: held.append(route))
        page.goto(page.url, wait_until='commit')
        page.locator('#boot-mark').wait_for(state='visible')
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOTS / f'inicio-{theme}.png'))
        assert page.locator('#boot-mark').inner_text() == 'Michiflin AI'
        assert page.evaluate("!window.__michiflinBootReady")
        page.wait_for_function("document.querySelector('#boot-mark') !== null")
        assert len(held) == 1
        held[0].continue_()
        page.wait_for_function('window.__michiflinBootReady === true')
        page.locator('#boot-mark').wait_for(state='detached')
        assert page.locator('#view').inner_text().strip()
        assert errors == []


def test_retired_store_cleanup_is_exact_opt_in_and_preserves_other_data(tmp_path, monkeypatch):
    from tools.retire_unused_ui_store import retire
    files = ['text-social.sqlite3', 'text-social.sqlite3-wal', 'text-social.sqlite3-shm', 'text-social.sqlite3-journal']
    for name in files:
        (tmp_path/name).write_text('synthetic retired data')
    other = tmp_path/'other.sqlite3'; other.write_text('keep')
    monkeypatch.setenv('RUNNING_SHA', 'a'*40)
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', 'b'*40)
    monkeypatch.setenv('UI_RETIRE_BACKUP_VERIFIED', '1')
    assert retire(tmp_path) == 0
    monkeypatch.setenv('OWNER_REQUESTED_APP_REVISION', 'a'*40)
    monkeypatch.setenv('UI_RETIRE_BACKUP_VERIFIED', '0')
    assert retire(tmp_path) == 0
    monkeypatch.setenv('UI_RETIRE_BACKUP_VERIFIED', '1')
    assert retire(tmp_path) == 4
    assert other.read_text() == 'keep'
    assert retire(tmp_path) == 0
    (tmp_path/files[0]).symlink_to(other)
    with pytest.raises(ValueError):
        retire(tmp_path)
    assert other.read_text() == 'keep'


def test_retired_store_workflow_is_after_recovery_and_before_bot():
    source = (Path(__file__).resolve().parents[1] / '.github/workflows/michiflin.yml').read_text()
    recovery = source.index('- name: Verify backup recovery')
    cleanup = source.index('- name: Retire the removed UI live store')
    serve = source.index('- name: Private chats and restricted public group tools')
    assert recovery < cleanup < serve
    section = source[cleanup:source.index('- name:', cleanup + 10)]
    assert "env.OWNER_REQUESTED_APP_REVISION == github.sha" in section
    assert 'UI_RETIRE_BACKUP_VERIFIED: "1"' in section


@pytest.mark.parametrize('engine,width,height,locale,theme', MATRIX)
def test_remaining_screens_responsive_matrix(engine, width, height, locale, theme):
    """All other top-level routes, plus kit/links, use synthetic responses only."""
    target = SCREENSHOTS / f'extended-{engine}-{width}-{height}-{locale}-{theme}'
    target.mkdir(parents=True, exist_ok=True)
    with browser_fixture(locale, {'width': width, 'height': height}, engine=engine, touch=width < 800) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("value=>{prefs.theme=value;applyLook()}", theme)
        screens = [('help', {}), ('personal_ai', {}), ('images', {}), ('coder', {}),
                   ('results', {'q': 'synthetic'}), ('reader', {'url': 'https://example.org/fixture'}),
                   ('transcript', {'url': 'https://www.youtube.com/watch?v=abcdefghijk'}),
                   ('music', {}), ('music_item', {'id': 'album-1'}), ('weather', {}),
                   ('news', {}), ('translate', {}), ('tools', {}), ('health', {}), ('admin', {}),
                   ('notes', {}), ('links', {}), ('nauta_strict', {}), ('kill_switch', {}), ('estado', {}),
                   ('live_web', {'url': 'https://example.org/fixture'}),
                   ('downloads', {'section': 'kit'}), ('downloads', {'section': 'links'})]
        for index, (view, params) in enumerate(screens):
            page.evaluate('([view,params])=>go(view,params)', [view, params])
            page.wait_for_timeout(120)
            assert page.locator('#view').evaluate('e=>e.scrollWidth<=e.clientWidth+1'), (view, params)
            assert page.locator('#view').inner_text().strip(), (view, params)
            assert page.locator('#view .failure').count() == 0, (view, params)
            assert errors == [], (view, params, errors)
            page.screenshot(path=str(target / f'{index:02d}-{view}.png'))
