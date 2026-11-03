"""The generated attachment remains readable and inert in a real offline browser."""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from michiflin import answer_html


@pytest.mark.parametrize("width", [320, 375, 768, 1280])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_offline_mobile_and_desktop_layout(tmp_path: Path, width: int, scheme: str, engine: str):
    text = ("# Guía de ejemplo\n\nUna respuesta clara que puedes leer sin conexión. " * 5
            + "\n\n```html\n" + "<div>" + "x" * 450 + "</div>\n"
            + "<script>window.__unsafe = true</script>\n```\n\n"
            + '<img src="https://attacker.invalid/collect" onerror="window.__unsafe=true">\n'
            + "[S1] https://example.test/source\nFINAL DE LA RESPUESTA")
    path = tmp_path / "answer.html"
    path.write_text(answer_html.render(text, "es", signature="IA local", truncated=True), encoding="utf-8")
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme, offline=True)
        page = context.new_page()
        remote = []
        page.on("request", lambda request: remote.append(request.url) if request.url.startswith(("https:", "http:")) else None)
        page.goto(path.as_uri(), wait_until="load")
        assert page.locator("h1").inner_text() == "Tu respuesta"
        assert page.locator("article").inner_text().endswith("FINAL DE LA RESPUESTA")
        assert page.locator(".notice").is_visible() and "IA local" in page.locator(".attribution").inner_text()
        assert page.locator("script, img, a").count() == 0
        assert page.evaluate("window.__unsafe") is None
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("pre").evaluate("el => el.scrollWidth > el.clientWidth")
        assert remote == []
        context.close()
        browser.close()
