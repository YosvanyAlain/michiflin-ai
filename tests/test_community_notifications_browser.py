"""Responsive notification bell. No live bot or provider is contacted."""
import pytest

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
        assert page.locator('#notifications-toggle .notification-count').inner_text() == '2'
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
        page.get_by_role('button', name='Ocultas', exact=True).click()
        row = page.locator('.notice-center-row').filter(has_text='Resultado de otra sección')
        row.get_by_role('button', name='Mostrar', exact=True).click()
        assert page.locator('#notifications-toggle .notification-count').inner_text() == '1'
        page.get_by_role('button', name='Eliminar todas', exact=True).click()
        page.locator('#sheet').get_by_role('button', name='Eliminar', exact=True).click()
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
