"""Dialog regressions discovered in a local audit; the mail bridge is synthetic."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("dismiss", ["escape", "close", "scrim", "replace"])
def test_dismissing_confirmation_resolves_false(engine, dismiss):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.evaluate("""() => {
          window.__decision = 'pending';
          confirmBox('Confirmación de prueba', 'No hay datos reales.', 'Borrar')
            .then(value => { window.__decision = value; });
        }""")
        page.locator("#sheet.open").wait_for()
        if dismiss == "escape":
            page.keyboard.press("Escape")
        elif dismiss == "close":
            page.locator("#sheet .sheet-head button").click()
        elif dismiss == "scrim":
            page.locator("#sheet .sheet-scrim").click(position={"x": 4, "y": 4})
        else:
            page.evaluate("openSheet('Otra ventana', 'Sin acciones')")
        page.wait_for_function("window.__decision === false", timeout=1500)
        if dismiss == "replace":
            assert page.get_by_role("dialog", name="Otra ventana").is_visible()
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["es", "en"])
def test_sheet_is_named_modal_and_keeps_keyboard_focus(engine, language):
    with browser_fixture(language, {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.locator("#splash").wait_for(state="detached")
        page.locator("#gear").focus()
        page.evaluate("""openSheet('Prueba accesible', [
          h('input', {'aria-label': 'Texto de prueba'}),
          h('button', {id: 'audit-last'}, 'Último')
        ])""")
        dialog = page.get_by_role("dialog", name="Prueba accesible")
        assert dialog.get_attribute("aria-modal") == "true"
        page.wait_for_function("!!document.activeElement.closest('#sheet')")
        assert page.locator("#app").evaluate("node => node.inert")
        page.locator("#audit-last").focus()
        page.keyboard.press("Tab")
        assert page.locator("#sheet .sheet-head button").evaluate("node => node === document.activeElement")
        page.keyboard.press("Shift+Tab")
        assert page.locator("#audit-last").evaluate("node => node === document.activeElement")
        page.keyboard.press("Escape")
        page.locator("#sheet").wait_for(state="hidden")
        assert not page.locator("#app").evaluate("node => node.inert")
        assert page.locator("#gear").evaluate("node => node === document.activeElement")
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_replacing_closing_sheet_does_not_unlock_background_or_steal_focus(engine):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.locator("#splash").wait_for(state="detached")
        page.locator("#gear").focus()
        page.evaluate("""() => {
          openSheet('Anterior', 'Primero');
          closeSheet();
          openSheet('Nueva', h('button', {id:'audit-new'}, 'Nueva acción'));
        }""")
        page.wait_for_timeout(350)  # exceed the old close animation
        assert page.get_by_role("dialog", name="Nueva").is_visible()
        assert page.locator("#app").evaluate("node => node.inert")
        assert page.evaluate("!!document.activeElement.closest('#sheet')")
        page.keyboard.press("Escape")
        page.locator("#sheet").wait_for(state="hidden")
        assert page.locator("#gear").evaluate("node => node === document.activeElement")
        assert errors == []


@pytest.mark.parametrize("answer", [True, False])
def test_explicit_confirmation_choice_is_preserved(answer):
    with browser_fixture("es", {"width": 390, "height": 844}) as (page, errors):
        page.evaluate("""() => {
          window.__decision = 'pending';
          confirmBox('Confirmar', 'Solo prueba', 'Aceptar prueba')
            .then(value => { window.__decision = value; });
        }""")
        page.locator("#sheet .sheet-body button").nth(1 if answer else 0).click()
        page.wait_for_function("value => window.__decision === value", arg=answer)
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_long_sheet_title_keeps_close_control_on_screen(engine):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine, touch=True) as (page, errors):
        page.evaluate("openSheet('Proyecto'+'x'.repeat(100), 'Contenido de prueba')")
        page.locator('#sheet.open').wait_for()
        page.wait_for_timeout(350)
        bounds = page.locator('.sheet-head').evaluate("""node => {
          const close = node.querySelector('button').getBoundingClientRect();
          return {client: node.clientWidth, scroll: node.scrollWidth,
            closeLeft: close.left, closeRight: close.right, width: innerWidth};
        }""")
        assert bounds['scroll'] <= bounds['client'] + 1
        assert 0 <= bounds['closeLeft'] < bounds['closeRight'] <= bounds['width']
        assert errors == []


def test_viewport_does_not_forbid_user_zoom():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'webxdc/index.html').read_text()
    assert 'maximum-scale=1' not in source
    assert 'user-scalable=no' not in source
