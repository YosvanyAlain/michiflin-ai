"""Mobile Live controls and consent; all pages, images and credentials are synthetic.

Chromium/WebKit approximate clients but do not simulate a native phone keyboard,
WebXDC realtime transport, Google authentication, or a real user session.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_webxdc_browser import browser_fixture, install_live_frame_fixture


CASES = [
    ("es", "chromium", 280, 568),
    ("es", "chromium", 320, 568),
    ("es", "chromium", 360, 640),
    ("en", "webkit", 320, 568),
    ("es", "webkit", 390, 844),
]


def start_fixture(page, language="es"):
    install_live_frame_fixture(page)
    page.get_by_role("button", name="Iniciar navegador" if language == "es" else "Start browser").click()
    page.wait_for_function("window.liveWeb.ready && document.querySelector('.live-screen')?.naturalWidth === 480")
    page.locator(".live-screen-box:not([aria-busy])").wait_for()
    page.wait_for_function("""() => {
      const work = document.querySelector('.live-workspace'), main = document.querySelector('#view');
      return work && Math.abs(work.getBoundingClientRect().top - main.getBoundingClientRect().top) < 3;
    }""")


def wait_idle(page):
    page.wait_for_function("window.liveWeb.ready && !window.liveWeb.pending")
    page.locator(".live-screen-box:not([aria-busy])").wait_for()


def rectangles(page):
    return page.evaluate("""() => {
      const read = selector => {
        const r = document.querySelector(selector).getBoundingClientRect();
        return {left:r.left, top:r.top, right:r.right, bottom:r.bottom, width:r.width, height:r.height};
      };
      return {main:read('#view'), work:read('.live-workspace'), nav:read('.live-navigation'),
        image:read('.live-screen'), box:read('.live-screen-box'), input:read('.live-compose-text input'),
        send:read('.live-compose-text button'), keys:read('.live-compose-keys')};
    }""")


def assert_fits_visible_view(rects, viewport):
    main = rects["main"]
    for name in ("nav", "image", "input", "send", "keys"):
        r = rects[name]
        assert r["width"] > 0 and r["height"] > 0, (name, rects)
        assert r["top"] >= max(0, main["top"]) - 2, (name, rects)
        assert r["bottom"] <= min(viewport["height"], main["bottom"]) + 2, (name, rects)
        assert r["left"] >= -1 and r["right"] <= viewport["width"] + 1, (name, rects)
    assert rects["image"]["height"] >= (40 if viewport["height"] < 411 else 60), rects


@pytest.mark.parametrize("language,engine,width,height", CASES)
def test_view_and_typing_fit_together_on_small_screens(language, engine, width, height):
    viewport = {"width": width, "height": height}
    with browser_fixture(language, viewport, engine=engine, touch=True) as (page, errors):
        start_fixture(page, language)
        assert_fits_visible_view(rectangles(page), viewport)
        assert page.locator(".live-screen-box").evaluate("e => e.scrollHeight - e.clientHeight") <= 1
        labels = (["Página anterior", "Página siguiente", "Recargar página", "Actualizar vista"] if language == "es" else
                  ["Previous page", "Next page", "Reload page", "Refresh view"])
        for label in labels:
            button = page.get_by_role("button", name=label, exact=True)
            box = button.bounding_box()
            assert box["width"] >= 40 and box["height"] >= 40, (label, box)
        link_name = "Abrir dirección inicial en el navegador del dispositivo" if language == "es" else "Open initial address in the device browser"
        assert page.get_by_role("link", name=link_name).count() == 0
        assert page.locator(".live-device-link").count() == 0
        page.locator('.live-privacy > summary').click()
        privacy = page.locator('.live-privacy').inner_text()
        assert ("un lector de pantalla no puede leer" if language == "es" else "a screen reader cannot read") in privacy
        assert ("no se copia al bot" if language == "es" else "not copied to the bot") in privacy
        page.locator('.live-privacy > summary').click()
        status_label = "Estado del navegador; desplázate para leerlo completo" if language == "es" else "Browser status; scroll to read it in full"
        assert page.get_by_role("region", name=status_label).count() == 1
        destination = os.environ.get("MICHIFLIN_SCREENSHOT_DIR")
        if destination:
            Path(destination).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(destination) / f"SIMULADO-live-{language}-{engine}-{width}x{height}.png"))
        assert errors == []


def test_mobile_navigation_typing_and_software_keyboard_resize():
    viewport = {"width": 360, "height": 640}
    with browser_fixture("es", viewport, engine="chromium", touch=True) as (page, errors):
        start_fixture(page)
        for label, action in (("Página anterior", "back"), ("Página siguiente", "forward"),
                              ("Recargar página", "reload"), ("Actualizar vista", "frame"),
                              ("Subir un poco en la página", "scroll"), ("Bajar un poco en la página", "scroll")):
            before = page.evaluate("window.__testLiveActions.length")
            page.get_by_role("button", name=label, exact=True).click()
            page.wait_for_function("n => window.__testLiveActions.length > n", arg=before)
            wait_idle(page)
            assert page.evaluate("window.__testLiveActions.at(-1).action") == action
        field = page.get_by_label("Texto o contraseña para enviar a la página")
        assert field.get_attribute("enterkeyhint") == "send"
        field.fill("texto de prueba sin cuenta")
        field.press("Enter")
        page.wait_for_function("window.__testLiveActions.some(x => x.action === 'text')")
        wait_idle(page)
        assert field.input_value() == ""
        assert page.evaluate("window.__testLiveActions.filter(x => x.action === 'text').at(-1).text") == "texto de prueba sin cuenta"
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'key' && x.key === 'Enter')")
        assert not page.evaluate("JSON.stringify(window.__sentUpdates).includes('texto de prueba sin cuenta')")
        assert not page.evaluate("JSON.stringify(localStorage).includes('texto de prueba sin cuenta')")
        field.focus()
        for height in (410, 360, 640):
            # Models a resized browser viewport, not a real Android/iOS keyboard.
            viewport = {"width": 360, "height": height}
            page.set_viewport_size(viewport)
            page.wait_for_timeout(100)
            assert_fits_visible_view(rectangles(page), viewport)
        # Model a visual viewport shifted below the layout viewport, as can
        # happen with a virtual keyboard. This is still a synthetic browser test.
        page.evaluate("""() => {
          Object.defineProperty(window.visualViewport, 'offsetTop', {configurable:true, value:100});
          window.visualViewport.dispatchEvent(new Event('resize'));
        }""")
        page.wait_for_function("Math.abs(document.querySelector('.live-workspace').getBoundingClientRect().top - 100) < 3")
        assert_fits_visible_view(rectangles(page), viewport)
        page.evaluate("""() => {
          delete window.visualViewport.offsetTop;
          window.visualViewport.dispatchEvent(new Event('resize'));
        }""")
        assert errors == []


def test_text_validation_and_busy_step_preserve_unsent_input():
    with browser_fixture("es", {"width": 360, "height": 640}) as (page, errors):
        start_fixture(page)
        field = page.get_by_label("Texto o contraseña para enviar a la página")
        field.fill("x" * 4097)
        field.press("Enter")
        assert field.input_value() == "x" * 4097
        assert "no se envió" in page.locator(".live-work-status").inner_text()
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'text')")
        # Under 4096 Unicode characters, but over the 15 KB realtime payload limit.
        large_unicode = "😀" * 3800
        field.fill(large_unicode)
        field.press("Enter")
        assert field.input_value() == large_unicode
        assert "límite" in page.locator(".live-work-status").inner_text()
        assert page.evaluate("window.liveWeb.ready && !window.liveWeb.pending")
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'text')")
        # A request in flight must not consume a newly typed value either.
        page.evaluate("window.__testLiveDelay = 900")
        page.get_by_role("button", name="Actualizar vista", exact=True).click()
        field.fill("texto posterior")
        page.get_by_role("button", name="Enviar texto").click()
        assert field.input_value() == "texto posterior"
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'text')")
        wait_idle(page)
        page.get_by_role("button", name="Enviar texto").click()
        wait_idle(page)
        assert field.input_value() == ""
        assert page.evaluate("window.__testLiveActions.filter(x => x.action === 'text').length") == 1
        assert errors == []


@pytest.mark.parametrize("language,engine,width,height", [
    ("es", "chromium", 360, 360), ("en", "webkit", 320, 360),
    ("es", "chromium", 280, 360), ("es", "chromium", 280, 568),
])
def test_long_error_keeps_controls_reachable_in_a_reduced_viewport(language, engine, width, height):
    with browser_fixture(language, {"width": width, "height": 640}, engine=engine) as (page, errors):
        start_fixture(page, language)
        field_label = "Texto o contraseña para enviar a la página" if language == "es" else "Text or password to send to the page"
        field = page.get_by_label(field_label)
        field.fill("😀" * 3800)
        viewport = {"width": width, "height": height}
        page.set_viewport_size(viewport)
        page.wait_for_timeout(100)
        field.press("Enter")
        status_label = "Estado del navegador; desplázate para leerlo completo" if language == "es" else "Browser status; scroll to read it in full"
        status = page.get_by_role("region", name=status_label)
        assert ("límite" if language == "es" else "limit") in status.inner_text()
        assert_fits_visible_view(rectangles(page), viewport)
        # This common two-line notice should fit without scrolling, even in
        # a short viewport; all six compact navigation targets remain tappable.
        assert status.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        for button in page.locator(".live-navigation button").all():
            box = button.bounding_box()
            assert box["width"] >= 40 and box["height"] >= 40, box
        destination = os.environ.get("MICHIFLIN_SCREENSHOT_DIR")
        if destination:
            Path(destination).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(destination) / f"SIMULADO-live-aviso-{language}-{engine}-{width}x{height}.png"))
        # Model an actual longer server error in response to a pending step.
        page.evaluate("""() => {
          window.__testLiveDelay = 3000;
          if (!window.liveWeb.action('frame')) throw new Error('fixture action not accepted');
          const live = window.liveWeb;
          window.__testLiveRespond({v:1, sid:live.sid, t:'error', seq:live.pending.seq, error:'proxy_unavailable'});
        }""")
        assert "proxy" in status.inner_text().lower()
        assert_fits_visible_view(rectangles(page), viewport)
        assert status.evaluate("e => e.scrollHeight > e.clientHeight")
        status.focus()
        page.keyboard.press("ArrowDown")
        page.wait_for_function("document.querySelector('.live-work-status').scrollTop > 0")
        assert errors == []


@pytest.mark.parametrize("language,engine", [("es", "chromium"), ("en", "webkit")])
def test_save_requires_confirmation_and_failure_is_not_reported_as_success(language, engine):
    with browser_fixture(language, {"width": 360, "height": 640}, engine=engine) as (page, errors):
        start_fixture(page, language)
        page.locator('.live-extra-controls > summary').click()
        page.locator('.live-session-options > summary').click()
        name = "Guardar sesión" if language == "es" else "Save session"
        cancel = "Cancelar" if language == "es" else "Cancel"
        save = page.locator("#view").get_by_role("button", name=name, exact=True)
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'save')")
        save.click()
        prompt = page.locator("#sheet [role=dialog]").inner_text()
        assert ("sitio inicial" if language == "es" else "initial site") in prompt
        assert ("accesos externos como Google" if language == "es" else "external sign-ins such as Google") in prompt
        page.get_by_role("button", name=cancel, exact=True).click()
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'save')")
        page.evaluate("window.__testLiveSaveError = 'vault'")
        save.click()
        page.locator("#sheet .btn.danger").click()
        page.wait_for_function("window.__testLiveActions.filter(x => x.action === 'save').length === 1")
        page.wait_for_function("!window.liveWeb.pending")
        expected_error = "No se pudo leer o guardar la sesión cifrada" if language == "es" else "Could not read or save the encrypted state"
        assert expected_error in page.locator(".live-work-status").inner_text()
        not_stored = "No hay estado de navegador guardado" if language == "es" else "No browser state is saved"
        assert not_stored in page.locator("#view").inner_text()
        page.evaluate("window.__testLiveSaveError = ''")
        save.click()
        page.locator("#sheet .btn.danger").click()
        page.wait_for_function("window.__testLiveActions.filter(x => x.action === 'save').length === 2")
        page.wait_for_function("!window.liveWeb.pending")
        expected_success = "Estado cifrado guardado" if language == "es" else "Encrypted state saved"
        assert expected_success in page.locator(".live-work-status").inner_text()
        assert errors == []


def test_expired_session_cannot_save_from_an_old_confirmation():
    with browser_fixture("es", {"width": 360, "height": 640}) as (page, errors):
        start_fixture(page)
        page.locator('.live-extra-controls > summary').click()
        page.get_by_role("button", name="Guardar sesión", exact=True).click()
        page.evaluate("""() => window.__testLiveRespond({v:1, sid:window.liveWeb.sid, t:'closed', reason:'expired'})""")
        page.locator("#sheet .btn.danger").click()
        assert not page.evaluate("window.__testLiveActions.some(x => x.action === 'save')")
        assert "sesión cambió mientras confirmabas" in page.locator("#view [role=status]").inner_text()
        assert errors == []


def test_keyboard_focus_common_keys_and_secret_visibility_are_accessible():
    with browser_fixture("es", {"width": 360, "height": 640}) as (page, errors):
        start_fixture(page)
        first = page.get_by_role("button", name="Página anterior", exact=True)
        first.focus()
        page.keyboard.press("Tab")
        assert page.get_by_role("button", name="Página siguiente", exact=True).evaluate("e => document.activeElement === e")
        field = page.get_by_label("Texto o contraseña para enviar a la página")
        reveal = page.get_by_role("button", name="Mostrar / ocultar")
        assert field.get_attribute("type") == "password"
        reveal.click()
        assert reveal.get_attribute("aria-pressed") == "true" and field.get_attribute("type") == "text"
        reveal.click()
        assert reveal.get_attribute("aria-pressed") == "false" and field.get_attribute("type") == "password"
        page.locator('.live-session-options > summary').click()
        quick = page.locator(".live-compose-keys button").nth(1)
        assert "Ajustar" in quick.get_attribute("aria-label")
        quick.click()
        assert "150%" in quick.get_attribute("aria-label")
        assert page.locator(".live-zoom").get_by_role("tab", name="150%").get_attribute("aria-selected") == "true"
        assert page.locator(".live-screen-box").evaluate("e => e.scrollHeight > e.clientHeight")
        quick.click()
        assert "200%" in quick.get_attribute("aria-label")
        quick.click()
        assert "Ajustar" in quick.get_attribute("aria-label")
        assert page.locator(".live-screen-box").evaluate("e => e.scrollHeight - e.clientHeight") <= 1
        page.get_by_role("button", name="Tecla Tab", exact=True).click()
        page.wait_for_function("window.__testLiveActions.some(x => x.action === 'key' && x.key === 'Tab')")
        wait_idle(page)
        page.get_by_role("button", name="Tecla Enter", exact=True).click()
        page.wait_for_function("window.__testLiveActions.some(x => x.action === 'key' && x.key === 'Enter')")
        wait_idle(page)
        assert errors == []
