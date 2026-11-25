"""Creator-family finish: browser-only layout/UX; no production or mail claims."""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("language", ["es", "en"])
def test_home_actions_and_progressive_search(engine, language):
    with browser_fixture(language, {"width": 320, "height": 568}, engine=engine) as (page, errors):
        page.emulate_media(reduced_motion="reduce")
        page.locator("#splash").wait_for(state="detached")
        assert page.locator(".home-brand-mark").count() == 0
        # The first slide contains only the countdown; the agent remains
        # available through its ordinary shortcut rather than a second card.
        assert page.locator(".home-countdown [data-restart-countdown]").count() == 1
        assert page.locator('.home-banner-controls button').first.get_attribute('aria-current') == 'true'
        page.locator(".tile-ai").click()
        assert page.evaluate("current.view") == "chat"
        assert page.evaluate("current.params.space") == "agent"
        page.evaluate("goTab('home')")
        assert not page.locator(".home-search-options").evaluate("node => node.open")
        page.locator(".home-search-options > summary").click()
        page.locator('.search-selectors [data-key="advanced"]').click()
        page.locator('.search-selectors [data-key="wiki"]').click()
        page.locator(".search-field input").fill("Prueba de búsqueda")
        page.locator(".search-field input").press("Enter")
        assert page.evaluate("current.view") == "results"
        assert page.evaluate("current.params.mode") == "advanced"
        assert page.evaluate("current.params.engine") == "wiki"
        assert not page.locator(".home-brand-mark").count()
        page.wait_for_function("store.get('notices',[]).some(n=>n.target?.view==='results')")
        page.evaluate("goTab('home')")
        page.locator('#notifications-toggle').click()
        page.locator('.notice-center-main').first.click()
        assert page.evaluate("current.view") == "results"
        page.evaluate("goTab('home')")
        page.locator('#tabs .tab[data-tab="downloads"]').click()
        assert page.evaluate("current.view") == "downloads"
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("theme", ["light", "dark", "system"])
def test_home_narrow_and_desktop_have_no_horizontal_overflow(engine, theme):
    with browser_fixture("es", {"width": 320, "height": 568}, engine=engine) as (page, errors):
        page.emulate_media(reduced_motion="reduce", color_scheme="dark")
        page.locator("#splash").wait_for(state="detached")
        page.evaluate("theme => { prefs.theme=theme; savePrefs(); applyLook(); }", theme)
        for width in [320, 390, 768, 1280]:
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(60)
            for name in [".hero", ".home-status", ".searchbox", ".tiles"]:
                assert page.locator(name).evaluate("e => e.scrollWidth <= e.clientWidth + 1"), (width, theme, name)
            assert page.locator(".home-countdown").bounding_box()["height"] >= 44
        # Rotating to shorter saved-data banners must not move the search box.
        before = page.locator(".searchbox").bounding_box()["y"]
        page.locator(".home-banner-controls button").nth(1).click()
        assert abs(page.locator(".searchbox").bounding_box()["y"] - before) <= 2
        page.locator(".home-banner-controls button").nth(2).click()
        assert abs(page.locator(".searchbox").bounding_box()["y"] - before) <= 2
        page.locator(".home-banner-controls button").nth(0).click()
        # Text zoom/reflow: allow vertical growth, never clip labels/actions.
        page.set_viewport_size({"width": 320, "height": 568})
        page.add_style_tag(content="body {font-size: 20px} .home-countdown-value {font-size: 38px}")
        assert page.locator(".home-hero").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        assert errors == []
