"""Offline UI tool: save a full-page screenshot of every tab.

Not part of the bot. A layout change is easy to reason about and easy to get
wrong; this renders each tab through the same fake Delta bridge the browser
tests use, so the result can be looked at instead of imagined.

Usage: PYTHONPATH=. python3 tools/ui_shots.py --out /tmp/shots [--theme dark]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tests.test_webxdc_browser import browser_fixture

TABS = ["home", "ai", "web", "apps", "downloads", "settings"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/shots")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--language", default="es")
    parser.add_argument("--theme", default="")
    parser.add_argument("--tag", default="")
    options = parser.parse_args()

    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    with browser_fixture(options.language, {"width": options.width, "height": options.height},
                         engine="chromium", touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        if options.theme:
            page.evaluate(f"document.documentElement.dataset.theme = {options.theme!r}")
            page.wait_for_timeout(150)
        found = page.evaluate("[...document.querySelectorAll('.tab')].map(t => t.dataset.tab || '')")
        tabs = sorted({t for t in found if t}) or TABS
        for tab in tabs:
            try:
                page.evaluate(f"goTab({tab!r})")
                page.wait_for_timeout(300)
            except Exception as exc:
                print(f"no se pudo abrir {tab}: {exc}")
                continue
            name = f"{options.tag + '_' if options.tag else ''}{tab}.png"
            page.screenshot(path=str(out / name))
            saved.append(name)
        if errors:
            print(f"errores de consola: {errors[:3]}")
    print(f"guardadas {len(saved)} capturas en {out}: {', '.join(saved)}")
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
