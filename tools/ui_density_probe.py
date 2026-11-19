"""Offline UI probe: measure how much scrolling each tab costs.

Not part of the bot. "Too much scrolling" is a feeling until it is a number,
so this walks every tab at phone size and reports how many screenfuls each
one is, how far down the first control sits, and how the height is spent.
Run it before and after a layout change to see whether the change helped.

Usage: PYTHONPATH=. python3 tools/ui_density_probe.py [--width 390]
"""
from __future__ import annotations

import argparse
import sys

from tests.test_webxdc_browser import browser_fixture

MEASURE = """
() => {
  // The app scrolls an inner <main>, not the document: measuring the document
  // reports the viewport height for every tab and hides the whole problem.
  const doc = document.querySelector('#view') || document.scrollingElement;
  const vh = doc.clientHeight;
  const sections = [...document.querySelectorAll('.sec, section')]
    .filter(s => s.offsetParent !== null);
  const controls = [...document.querySelectorAll('button, input, select, [role=button]')]
    .filter(c => c.offsetParent !== null);
  const edge = doc.getBoundingClientRect().bottom;
  const below = controls.filter(c => c.getBoundingClientRect().top > edge).length;
  const tall = sections
    .map(s => ({ name: (s.className || 'section').split(/\\s+/)[0],
                 h: Math.round(s.getBoundingClientRect().height) }))
    .sort((a, b) => b.h - a.h).slice(0, 3);
  return {
    scroll: Math.round(doc.scrollHeight),
    viewport: vh,
    sections: sections.length,
    controls: controls.length,
    hidden_controls: below,
    tallest: tall,
  };
}
"""

TABS = ["home", "ai", "web", "apps", "downloads", "settings"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--language", default="es")
    options = parser.parse_args()

    rows = []
    with browser_fixture(options.language, {"width": options.width, "height": options.height},
                         engine="chromium", touch=True) as (page, errors):
        page.wait_for_function("cache.at > 0")
        found = page.evaluate("[...document.querySelectorAll('.tab')].map(t => t.dataset.tab || '')")
        tabs = sorted({t for t in found if t}) or TABS
        for tab in tabs:
            try:
                page.evaluate(f"goTab({tab!r})")
                page.wait_for_timeout(260)
            except Exception as exc:
                print(f"no se pudo abrir {tab}: {exc}")
                continue
            data = page.evaluate(MEASURE)
            data["tab"] = tab
            rows.append(data)
        if errors:
            print(f"errores de consola: {errors[:3]}")

    if not rows:
        print("la sonda no abrio ninguna vista: el resultado no vale")
        return 1

    width = options.width
    print(f"\nDensidad a {width}x{options.height} ({options.language})")
    print(f"{'pestana':<12}{'alto':>7}{'pantallas':>11}{'secs':>6}{'ctrls':>7}"
          f"{'ocultos':>9}  seccion mas alta")
    print("-" * 78)
    worst = []
    for row in sorted(rows, key=lambda r: -r["scroll"]):
        screens = row["scroll"] / row["viewport"]
        top = row["tallest"][0] if row["tallest"] else {"name": "-", "h": 0}
        print(f"{row['tab']:<12}{row['scroll']:>7}{screens:>10.1f}x{row['sections']:>6}"
              f"{row['controls']:>7}{row['hidden_controls']:>9}  {top['name']} ({top['h']}px)")
        if screens > 3:
            worst.append((row["tab"], screens))
    total = sum(r["scroll"] for r in rows) / max(len(rows), 1)
    print("-" * 78)
    print(f"media: {total:.0f}px  ·  {total / options.height:.1f} pantallas por pestana")
    if worst:
        print("\nPor encima de 3 pantallas (candidatas a reorganizar):")
        for tab, screens in sorted(worst, key=lambda x: -x[1]):
            print(f"  {tab}: {screens:.1f} pantallas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
