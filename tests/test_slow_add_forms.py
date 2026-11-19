"""Slow-mail form regressions, with synthetic delayed replies.

The note draft loss was also reproduced through real Delta Chat mail against a
local QA bot. These tests cover notes and the equivalent saved-links form.
"""
from __future__ import annotations

import pytest

from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("view,operation,first,second", [
    ("notes", "notas", "Primera nota", "Segundo borrador"),
    ("links", "enlaces", "https://example.org/first", "https://example.org/second"),
])
@pytest.mark.parametrize("change_draft", [False, True])
def test_add_is_single_flight_and_does_not_erase_a_new_draft(engine, view, operation, first, second, change_draft):
    with browser_fixture("es", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.evaluate("view => go(view)", view)
        page.wait_for_function("Object.keys(bridge.pending()).length===0")
        page.evaluate("window.__delay=1200")
        field = page.locator("#view input")
        button = page.locator("#view .search-field button")
        field.fill(first)
        field.press("Enter")
        assert button.is_disabled()
        field.press("Enter")  # Enter must obey the same guard as the button.
        if change_draft:
            field.fill(second)
        page.wait_for_function("op=>!Object.values(bridge.pending()).some(p=>p.op===op)", arg=operation)
        assert field.input_value() == (second if change_draft else "")
        assert button.is_enabled()
        assert page.evaluate("op=>window.__sentUpdates.filter(x=>x.payload?.op===op&&x.payload.args.do==='add').length", operation) == 1
        assert errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("view,operation,text", [
    ("notes", "notas", "Una nota que no debe perderse"),
    ("links", "enlaces", "https://example.org/keep-on-failure"),
])
def test_failed_add_keeps_text_and_unlocks_retry(engine, view, operation, text):
    with browser_fixture("en", {"width": 390, "height": 844}, engine=engine) as (page, errors):
        page.wait_for_function("cache.at>0&&!syncing")
        page.evaluate("view=>go(view)", view)
        page.wait_for_function("Object.keys(bridge.pending()).length===0")
        page.evaluate("op=>{window.__delay=500;window.__failNext[op]=1;}", operation)
        field = page.locator("#view input")
        button = page.locator("#view .search-field button")
        field.fill(text)
        button.click()
        assert button.is_disabled()
        page.wait_for_function("op=>!Object.values(bridge.pending()).some(p=>p.op===op)", arg=operation)
        assert field.input_value() == text
        assert button.is_enabled()
        button.click()
        page.wait_for_function("op=>!Object.values(bridge.pending()).some(p=>p.op===op)", arg=operation)
        assert field.input_value() == ""
        assert errors == []
