"""Synthetic community + responsive bell. No live bot or provider is contacted."""
import pytest

from michiflin import config, text_social
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,width,height', [('chromium', 1280, 800), ('webkit', 390, 844), ('chromium', 320, 568)])
def test_bell_general_notices_paint_clear_hide_and_bottom_badges(engine, width, height):
    with browser_fixture('es', {'width': width, 'height': height}, engine=engine) as (page, errors):
        page.evaluate("""() => {notice('Aviso general de prueba');
          notice('Resultado de otra sección','info',{category:'web',target:{view:'results',params:{q:'pendiente',engine:'web'}}});}""")
        page.wait_for_timeout(150)
        assert page.evaluate("store.get('notices',[]).every(n=>n.unread)")
        assert page.locator('#notifications-toggle .notification-count').inner_text() == '2'
        assert page.locator('.tab[data-tab="home"]:visible .tab-badge').is_hidden()
        assert page.locator('.tab[data-tab="web"]:visible .tab-badge').inner_text() == '1'
        assert page.locator('#view .notice').count() == 0
        page.locator('#notifications-toggle').click()
        page.wait_for_timeout(350)
        page.locator('.notice-center-row').filter(has_text='Aviso general de prueba').scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).find(n=>!n.target).unread")
        assert page.evaluate("store.get('notices',[]).find(n=>n.target).unread")
        panel = page.locator('#sheet .sheet-panel').bounding_box()
        assert panel['x'] >= 0 and panel['x'] + panel['width'] <= width + 1
        assert panel['y'] >= 0 and panel['y'] + panel['height'] <= height + 1
        row = page.locator('.notice-center-row').filter(has_text='Resultado de otra sección')
        row.get_by_role('button', name='Ocultar', exact=True).click()
        assert row.count() == 0
        assert page.locator('.tab[data-tab="web"]:visible .tab-badge').is_hidden()
        page.get_by_label('Mostrar ocultas').check()
        row = page.locator('.notice-center-row').filter(has_text='Resultado de otra sección')
        row.get_by_role('button', name='Mostrar', exact=True).click()
        assert page.locator('.tab[data-tab="web"]:visible .tab-badge').inner_text() == '1'
        page.get_by_role('button', name='Limpiar visibles', exact=True).click()
        page.locator('#sheet').get_by_role('button', name='Vaciar', exact=True).click()
        page.wait_for_function("store.get('notices',[]).length===0")
        assert page.locator('#notifications-toggle .notification-count').is_hidden()
        assert errors == []


def test_bell_not_read_while_closed_or_only_fleetingly_painted():
    with browser_fixture('es', {'width': 390, 'height': 600}) as (page, errors):
        page.evaluate("""() => {notice('Aviso fugaz');notificationCenter();closeSheet();}""")
        page.wait_for_timeout(350)
        assert page.evaluate("store.get('notices',[])[0].unread")
        page.locator('#notifications-toggle').click()
        page.wait_for_function("!store.get('notices',[])[0].unread")
        assert errors == []


def test_bell_offscreen_notices_remain_unread_until_scrolled():
    with browser_fixture('es', {'width': 390, 'height': 600}) as (page, errors):
        page.evaluate("""() => {for(let i=0;i<15;i++) notice('Texto de aviso '+i+' '+ 'contenido '.repeat(10));}""")
        page.locator('#notifications-toggle').click()
        page.wait_for_timeout(300)
        assert page.evaluate("store.get('notices',[]).find(n=>n.text.startsWith('Texto de aviso 0 ')).unread")
        page.locator('.notice-center-row').filter(has_text='Texto de aviso 0 ').scroll_into_view_if_needed()
        page.wait_for_function("!store.get('notices',[]).find(n=>n.text.startsWith('Texto de aviso 0 ')).unread")
        assert errors == []


@pytest.mark.parametrize('engine,language', [('chromium', 'es'), ('webkit', 'en')])
def test_real_social_handler_text_only_profile_draft_and_no_duplicate(monkeypatch, tmp_path, engine, language):
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(text_social, '_scope', lambda address: 'synthetic_verified_scope')
    monkeypatch.setattr(config, 'is_owner_admin', lambda address: True)
    def handler(request):
        assert request['op'] == 'social'
        return text_social.operate('synthetic@private.invalid', request['args'])
    with browser_fixture(language, {'width': 390, 'height': 844}, engine=engine, delivery_handler=handler) as (page, errors):
        page.evaluate('go("social",{})')
        page.locator('.social-composer textarea').wait_for()
        profile = 'Mi perfil' if language == 'es' else 'My profile'
        page.get_by_role('button', name=profile, exact=True).click()
        page.get_by_label('Alias público (no correo)' if language == 'es' else 'Public alias (not email)').fill('Luna')
        page.get_by_role('button', name='Guardar perfil y aspecto' if language == 'es' else 'Save profile and appearance').click()
        page.wait_for_function("document.querySelector('#sheet').hidden")
        page.wait_for_function("document.querySelector('.social-composer').textContent.includes('Luna')")
        area = page.locator('.social-composer textarea')
        text = '<img src=x onerror=alert(1)>\nUn texto con #etiqueta'
        area.fill(text)
        page.get_by_role('button', name='Actualizar' if language == 'es' else 'Refresh', exact=True).click()
        page.wait_for_timeout(200)
        assert area.input_value() == text
        page.get_by_role('button', name='Publicar' if language == 'es' else 'Post', exact=True).dblclick()
        page.wait_for_function("document.querySelectorAll('.social-post').length===1")
        assert page.locator('.social-post .social-text').inner_text() == text
        assert page.locator('.social-post img, .social-post iframe, .social-post video').count() == 0
        requests = page.evaluate("window.__sentUpdates.filter(x=>x.payload?.op==='social' && x.payload.args.do==='post').length")
        assert requests == 1
        assert area.input_value() == ''
        assert page.evaluate("document.querySelector('#view').scrollWidth<=document.querySelector('#view').clientWidth+1")
        assert 'private.invalid' not in page.locator('.social-space').inner_text()
        assert errors == []


def test_confirmed_profile_is_displayed_even_if_followup_feed_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(text_social, '_scope', lambda address: 'synthetic_verified_scope')
    monkeypatch.setattr(config, 'is_owner_admin', lambda address: True)
    saved = []
    def handler(request):
        if request['args']['do'] == 'feed' and saved:
            raise RuntimeError('Synthetic feed offline after confirmed profile')
        result = text_social.operate('synthetic@private.invalid', request['args'])
        if request['args']['do'] == 'profile':
            saved.append(True)
        return result
    with browser_fixture('en', {'width': 390, 'height': 844}, engine='webkit', delivery_handler=handler) as (page, errors):
        page.evaluate('go("social",{})')
        page.locator('.social-composer textarea').wait_for()
        page.locator('.social-composer textarea').fill('Draft that must survive the profile change')
        page.get_by_role('button', name='My profile', exact=True).click()
        page.get_by_label('Public alias (not email)').fill('Luna')
        page.get_by_role('button', name='Save profile and appearance').click()
        page.wait_for_function("document.querySelector('#sheet').hidden")
        page.wait_for_function("document.querySelector('.social-posting-as').textContent.includes('Luna')")
        page.wait_for_function("document.querySelector('.social-space [role=status]').textContent.includes('Synthetic feed offline')")
        assert page.locator('.social-composer textarea').input_value() == 'Draft that must survive the profile change'
        assert len(saved) == 1
        assert errors == []
