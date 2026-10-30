"""Bilingual mobile invitation controls backed by real local access handlers."""
from __future__ import annotations

import pytest

from michiflin import access, admin, community, config, miniapp, state
from tests.test_webxdc_browser import browser_fixture, screenshot_if_requested


@pytest.mark.parametrize("language,engine,width", [("es", "chromium", 320), ("en", "webkit", 375)])
def test_invitation_controls_are_bilingual_and_real(language, engine, width, monkeypatch):
    access.reset()
    for key, value in {
        "OWNER_ADDRESS": "owner@example.org", "OWNER_BACKUP_ADDRESS": "backup@example.org",
        "OWNER_ONLY": False, "INVITE_AUTO_ACCESS": True, "MAINTENANCE_MODE": False,
        "EMAIL_KEY_RECOVERY": True,
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setitem(state.data, "invite_policy", {"closed": True, "until": 0.0})
    monkeypatch.setitem(state.data, "invitees", {"guest@example.org": {
        "active": True, "banned": False, "chat": 21, "stamp": "a" * 32, "key": "b" * 64,
    }})
    monkeypatch.setitem(state.data, "access", {"allowed": ["historical@example.org"], "blocked": [], "attempts": {}, "alerts": True})
    monkeypatch.setattr(state, "save", lambda: None)
    monkeypatch.setattr(community, "_checkpoint", lambda: None)
    monkeypatch.setattr(miniapp.perf, "snapshot", lambda: None)
    monkeypatch.setattr(miniapp.usage, "report", lambda: None)
    monkeypatch.setattr(state, "maintenance_active", lambda: False)
    access.restore_policy()
    try:
        with browser_fixture(language, {"width": width, "height": 780}, real_access=True, engine=engine) as (page, errors):
            page.evaluate("go('admin')")
            card = page.locator(".owner-access")
            card.wait_for(state="visible")
            assert not admin.is_allowed("guest@example.org")
            assert page.locator(".single-guest").count() == 0
            assert card.locator("select").input_value() == "-1"
            hint = card.inner_text()
            assert ("invitación privada" if language == "es" else "private invitation") in hint.lower()
            assert ("administrador" if language == "es" else "administrator") in hint.lower()
            screenshot_if_requested(page, f"comunidad-{language}-cerrada")
            card.locator("button").click()
            page.locator("#sheet .btn.danger").click()
            page.wait_for_function("document.querySelector('.owner-access b')?.textContent.startsWith('0')")
            page.locator("#sheet").wait_for(state="hidden")
            card.scroll_into_view_if_needed()
            assert admin.is_allowed("guest@example.org") and not admin.is_admin("guest@example.org")
            assert not admin.is_allowed("historical@example.org")
            assert page.locator(".single-guest").count() == 0
            assert page.evaluate("document.querySelector('#view').scrollWidth <= document.querySelector('#view').clientWidth")
            screenshot_if_requested(page, f"comunidad-{language}-abierta")
            card.locator("button").click()
            page.locator("#sheet .btn.danger").click()
            page.wait_for_function("document.querySelector('.owner-access b')?.textContent.startsWith('1')")
            assert not admin.is_allowed("guest@example.org")
            access.reset()
            access.restore_policy()
            assert access.snapshot()["owner_only"] and not admin.is_allowed("guest@example.org")
            assert errors == []
    finally:
        access.reset()
