"""The unchanged miniapp displays useful inline parts and returns exact receipts."""
from __future__ import annotations

import pytest

from michiflin import answer_inline
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language,width", [("es", 320), ("en", 375)])
def test_existing_mobile_app_shows_long_text_and_continuation_without_html(engine, language, width):
    text = ("# Guía de ejemplo\n\n" + "Explicación útil de la huerta con detalles. " * 85
            + "\n\n```html\n" + "<div>plantas seguras</div>\n" * 350
            + "<script>window.__unsafe = true</script>\n```\n\n"
            + "[S1] https://example.test/fuente\nFINAL COMPLETO")
    pages = answer_inline.InlinePages()
    key = pages.key(91, "owner@example.test", "agent")
    first, paged = pages.start(key, {"text": text, "sig": "IA local"}, language)
    assert paged
    next_part = pages.take(key, [["assistant", first["text"]]], language)
    assert next_part and next_part["text"] != first["text"]
    with browser_fixture(language, {"width": width, "height": 812}, engine=engine) as (page, errors):
        page.evaluate("value => { window.__fixtureAskResult = value; }", first)
        page.locator('.tab[data-tab="ai"]:visible').click()
        page.get_by_text("Agente" if language == "es" else "Agent", exact=True).first.click()
        page.locator("#view textarea").fill("Explícame una huerta")
        page.locator("#view .composer button.primary").click()
        first_bubble = page.locator(".bubble.bot").last
        first_bubble.get_by_text("Guía de ejemplo").wait_for(state="visible")
        assert "Explicación útil" in first_bubble.inner_text()
        assert ("continúa" if language == "es" else "continue") in first_bubble.inner_text().lower()
        assert "HTML enviado" not in first_bubble.inner_text()
        assert first_bubble.locator("script, img").count() == 0
        assert page.evaluate("window.__unsafe") is None
        assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
        page.evaluate("value => { window.__fixtureAskResult = value; }", next_part)
        page.locator("#view textarea").fill("continúa" if language == "es" else "continue")
        page.locator("#view .composer button.primary").click()
        page.wait_for_function("window.__sentUpdates.filter(x => x.payload?.op === 'ask').length === 2")
        request = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'ask').at(-1).payload")
        assert any(turn[0] == "assistant" and turn[1] == first["text"] for turn in request["args"]["history"])
        page.wait_for_function("store.get('chat.agent').filter(item => item.role === 'assistant' && !item.pending).length === 2")
        assert page.locator(".bubble.bot").last.locator("script, img").count() == 0
        assert page.evaluate("window.__unsafe") is None and errors == []
