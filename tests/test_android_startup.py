"""Browser emulation is not a physical Android/WebView certification.

Exercise the shipped UI, real taps and storage/API failure modes, not delivery
alone. The backend bridge is synthetic and never uses mail or provider APIs.
"""
import functools
import http.server
import os
from pathlib import Path
import threading
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from michiflin import config, miniapp
from tests.test_webxdc_browser import BRIDGE, browser_fixture

DEVICES = [('chromium', 320, 640, True), ('chromium', 360, 800, True),
           ('chromium', 412, 915, True), ('chromium', 800, 360, True),
           ('chromium', 768, 1024, True), ('webkit', 390, 844, True),
           ('webkit', 1024, 768, True), ('chromium', 1280, 720, False),
           ('chromium', 1920, 1080, False), ('webkit', 1440, 900, False)]


def navigate(page):
    for tab in ['home', 'ai', 'web', 'apps', 'downloads']:
        page.locator('.tab[data-tab="' + tab + '"]:visible').first.click()
        page.wait_for_function('tab=>current.view===tab', arg=tab)
        assert page.locator('#view').inner_text().strip()
        assert page.locator('[data-ui-recovery]').count() == 0
        assert page.locator('#view').evaluate('el=>getComputedStyle(el).opacity') == '1'
    settings = page.locator('#gear')
    if not settings.is_visible():
        settings = page.locator('.tab[data-tab="settings"]:visible').first
    settings.click()
    page.locator('.settings-tile').first.wait_for()
    assert page.locator('.settings-tile').count() == 9
    for section in ['appearance', 'ai', 'navigation', 'bot', 'privacy', 'library', 'connection', 'support']:
        page.evaluate("section=>go('settings',{section})", section)
        assert page.locator('#view').inner_text().strip()
        assert page.locator('[data-ui-recovery]').count() == 0
    page.evaluate("go('tools')")
    page.locator('.utility-tile').first.click()
    assert page.locator('.utility-workspace').count() == 1
    assert page.locator('#view').evaluate('el=>el.scrollWidth<=el.clientWidth+1')


@pytest.mark.parametrize('engine,width,height,touch', DEVICES)
@pytest.mark.parametrize('locale', ['es', 'en'])
def test_device_start_and_tapped_navigation(engine, width, height, touch, locale):
    with browser_fixture(locale, {'width': width, 'height': height}, engine=engine, touch=touch) as (page, errors):
        navigate(page)
        assert page.locator('#startup-recovery').is_hidden()
        assert errors == []


@pytest.mark.parametrize('fault', [
    "Object.defineProperty(window,'speechSynthesis',{get(){throw Error('denied');}})",
    "Object.defineProperty(window,'speechSynthesis',{value:{cancel(){throw Error('unsupported');}}})",
    "Object.defineProperty(window,'speechSynthesis',{value:{}})",
    "Object.defineProperty(window,'speechSynthesis',{value:undefined})",
    "localStorage.setItem('mf.cache',JSON.stringify({jobs:null}));",
    "localStorage.setItem('mf.cache',JSON.stringify({jobs:[null,false,{}, {id:1,status:'done'}]}));",
    "localStorage.setItem('mf.notices','null');localStorage.setItem('mf.prefs','[]');localStorage.setItem('mf.books.coverCache','{}');",
])
def test_optional_api_and_invalid_cache_cannot_kill_all_tabs(fault):
    with browser_fixture('es', {'width':360,'height':800}, touch=True, init_script=fault) as (page, errors):
        navigate(page)
        assert errors == []


def test_render_failure_is_visible_recoverable_and_never_reexecutes_work():
    with browser_fixture('es', {'width':360,'height':800}, touch=True) as (page, errors):
        page.evaluate("store.set('nota-preservada',{text:'conservar'});window.sentBefore=__sentUpdates.length;VIEWS.broken={title:()=>{throw Error('private error contents');}};go('broken')")
        assert page.locator('[data-ui-recovery]').count() == 1
        assert 'private error contents' not in page.locator('#view').inner_text()
        assert page.evaluate('store.get("nota-preservada").text') == 'conservar'
        assert page.evaluate('__sentUpdates.length===sentBefore')
        page.locator('#view button').first.click()
        assert page.locator('.home-banner-slot').count() == 1
        assert errors == []


@pytest.fixture
def packaged_app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path / 'build')
    archive = miniapp.build()
    root = tmp_path / 'host'
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == set(miniapp.package_names())
        package.extractall(root)
    (root / 'webxdc.js').write_text(BRIDGE)
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, 'http://127.0.0.1:' + str(server.server_port)
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_packaged_script_failure_has_independent_local_recovery(packaged_app):
    root, url = packaged_app
    (root / 'app.js').write_text('const invalid = ;')
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(locale='en', viewport={'width':360,'height':800})
        page.goto(url)
        page.locator('#startup-recovery:not([hidden])').wait_for()
        assert 'could not start' in page.locator('#startup-recovery').inner_text()
        assert '/app' in page.locator('#startup-recovery').inner_text()
        browser.close()


def test_actual_old_chromium_engine_with_packaged_app(packaged_app):
    executable = os.environ.get('MICHIFLIN_OLD_CHROMIUM', '')
    if not executable or not Path(executable).is_file():
        pytest.skip('separately installed old engine required, not a user-agent substitution')
    _root, url = packaged_app
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable, args=['--no-sandbox'])
        page = browser.new_page(locale='es', viewport={'width':360,'height':800}, is_mobile=True, has_touch=True)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_function('cache.at>0 && !syncing && window.__michiflinStartupDone === true')
        # Modern Playwright's :visible helper itself requires checkVisibility,
        # absent in this old engine. Use actual mouse input at DOM coordinates,
        # without polyfilling/changing the application's browser APIs.
        for tab in ['home', 'ai', 'web', 'apps', 'downloads', 'settings']:
            selector = '#gear' if tab == 'settings' else '#tabs [data-tab="' + tab + '"]'
            rect = page.evaluate("selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width};}", selector)
            assert rect['w'] > 0
            page.mouse.click(rect['x'], rect['y'])
            page.wait_for_function('tab=>current.view===tab', arg=tab)
            assert page.evaluate("document.querySelector('#view').innerText.trim().length>0")
            assert page.evaluate("!document.querySelector('[data-ui-recovery]')")
        assert page.evaluate("document.querySelectorAll('.settings-tile').length") == 9
        page.evaluate("""() => {window.__accountResponse = request => {
          const action = request.args.do;
          if (action === 'status') return {configured:true,connected:false,stored:false};
          if (action === 'start') return {configured:true,connected:false,pending:true,user_code:'TEST-1234'};
          if (action === 'poll') return {configured:true,connected:true,pending:false};
          return {items:[]};
        }}""")
        def tap_node(selector, text=''):
            # Browser 100 lacks the newer checkVisibility helper used by
            # Playwright locators. Scroll a real control and use mouse input.
            rect = page.evaluate("""([selector,text]) => {
              const node = Array.from(document.querySelectorAll(selector)).find(e=>!text||e.textContent.includes(text));
              if (!node) return null;
              node.scrollIntoView({block:'center'});
              const r=node.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};
            }""", [selector, text])
            assert rect is not None, (selector, text)
            page.mouse.click(rect['x'], rect['y'])
        tap_node('[data-setting-section="privacy"]')
        tap_node('#view button', 'Conexiones oficiales')
        tap_node('[data-official-service="youtube"]')
        page.wait_for_function("document.querySelector('[data-official-action=\"start\"]')")
        tap_node('[data-official-action="start"]')
        page.wait_for_function("document.querySelector('.official-code code')?.textContent === 'TEST-1234'")
        tap_node('[data-official-action="poll"]')
        page.wait_for_function("document.querySelector('[data-official-action=\"channels\"]')")
        assert errors == []
        browser.close()


def test_host_listener_failure_keeps_local_interface_usable(packaged_app):
    root, url = packaged_app
    (root / 'webxdc.js').write_text(BRIDGE + ";window.webxdc.setUpdateListener=()=>{throw Error('host unavailable')};")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        page = browser.new_page(locale='es', viewport={'width':360,'height':800})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url)
        page.locator('.home-banner-slot').wait_for()
        page.locator('#tabs [data-tab="apps"]').click()
        assert page.locator('#view').inner_text().strip()
        assert page.locator('#startup-recovery').is_hidden()
        assert errors == []
        browser.close()


@pytest.mark.parametrize('locale', ['es', 'en'])
def test_manual_tour_matches_current_chat_and_delivery_policy(locale):
    with browser_fixture(locale, {'width':360,'height':800}) as (page, errors):
        assert page.locator('.quick-tour,#welcome-guidance').count() == 0
        page.evaluate("go('help')")
        page.get_by_role('button', name='Ver guía rápida' if locale == 'es' else 'Open quick guide').click()
        pages = []
        for _ in range(5):
            pages.append(page.locator('.quick-tour').inner_text())
            page.locator('.quick-tour .btn.primary').click()
        page.locator('.quick-tour').wait_for(state='detached')
        text = ' '.join(pages)
        assert '/app' in text and 'miniapp' in text
        assert ('campana' if locale == 'es' else 'bell') in text
        assert ('ciudad' if locale == 'es' else 'city') in text
        assert ('Kit de archivos' if locale == 'es' else 'File kit') in text
        assert page.evaluate("!window.__sentUpdates.some(x => x.payload?.op === 'guidance')")
        assert errors == []
