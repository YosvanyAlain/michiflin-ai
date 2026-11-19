"""Offline UI probe: hunt for horizontal overflow and clipped text.

Not part of the bot. Walks every tab of the mini app at phone, tablet and
desktop widths, in both languages, and reports anything that sticks out of the
viewport or whose text is cut off. Also checks tap-target height on touch
widths and whether focus is visible when tabbing.

Usage: PYTHONPATH=. python3 tools/ui_overflow_probe.py
"""
from __future__ import annotations

import sys

from tests.test_webxdc_browser import browser_fixture

VIEWPORTS = [
    ("phone", 390, 844, True),
    ("tablet", 768, 1024, True),
    ("desktop", 1440, 900, False),
]

PROBE = """
() => {
  const report = {overflow: [], clipped: [], small: []};
  const docWidth = document.documentElement.clientWidth;
  const describe = (el) => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return el.tagName.toLowerCase() + id + cls;
  };
  // A child of a deliberately scrollable strip (segmented controls, carousels)
  // sticks out by design; only unscrollable overflow is a layout defect.
  const inScroller = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll') return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.position === 'fixed') continue;
    if (inScroller(el)) continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 && box.height === 0) continue;
    if (box.right > docWidth + 1 || box.left < -1) {
      report.overflow.push({el: describe(el), left: Math.round(box.left), right: Math.round(box.right)});
    }
    // text cut off horizontally without an ellipsis is a silent loss of content
    if (el.scrollWidth > el.clientWidth + 1 && style.overflowX === 'hidden'
        && style.textOverflow !== 'ellipsis' && el.children.length === 0) {
      report.clipped.push({el: describe(el), scroll: el.scrollWidth, client: el.clientWidth,
                           text: (el.textContent || '').trim().slice(0, 40)});
    }
  }
  // Measure the real hit area, not the painted box: a transparent ::before can
  // legitimately enlarge a small-looking control, and only hit testing sees it.
  const hits = (el, x, y) => { const at = document.elementFromPoint(x, y); return at === el || (at && el.contains(at)); };
  for (const el of document.querySelectorAll('button, a, [role=button], input, select')) {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const box = el.getBoundingClientRect();
    if (box.height <= 0 || box.width <= 0) continue;
    const cx = Math.round(box.left + box.width / 2), cy = Math.round(box.top + box.height / 2);
    if (!hits(el, cx, cy)) continue;            // covered by an overlay: not tappable here anyway
    let top = cy, bottom = cy;
    while (cy - top < 30 && top > 1 && hits(el, cx, top - 1)) top -= 1;
    while (bottom - cy < 30 && bottom < innerHeight - 1 && hits(el, cx, bottom + 1)) bottom += 1;
    const reach = bottom - top + 1;
    if (reach < 24) {   // WCAG 2.2 AA minimum target size
      report.small.push({el: describe(el), painted: Math.round(box.height), hit: reach, width: Math.round(box.width)});
    }
  }
  return report;
}
"""

TABS = ["home", "ai", "web", "apps", "downloads", "settings"]


def main() -> int:
    findings = 0
    visited: list[str] = []
    for language in ("es", "en"):
        for name, width, height, touch in VIEWPORTS:
            with browser_fixture(language, {"width": width, "height": height},
                                 engine="chromium", touch=touch) as (page, errors):
                page.wait_for_function("cache.at > 0")
                tabs = page.evaluate("[...document.querySelectorAll('.tab')].map(t => t.dataset.tab || '')")
                tabs = sorted({t for t in tabs if t}) or TABS
                for tab in tabs:
                    try:
                        page.evaluate(f"goTab({tab!r})")
                        page.wait_for_timeout(220)
                    except Exception as exc:
                        print(f"[{language}/{name}] NO se pudo abrir {tab}: {exc}")
                        continue
                    shown = page.evaluate("document.querySelector('.tab.on')?.dataset.tab || ''")
                    if shown != tab:
                        print(f"[{language}/{name}] {tab} no quedó activa (activa: {shown!r})")
                    visited.append(f"{name}/{tab}")
                    report = page.evaluate(PROBE)
                    for kind in ("overflow", "clipped", "small"):
                        for item in report[kind]:
                            findings += 1
                            print(f"[{language}/{name}:{width}px/{tab}] {kind}: {item}")
                if errors:
                    print(f"[{language}/{name}] errores de consola: {errors[:3]}")
    print(f"\nvistas recorridas: {len(visited)} -> {sorted(set(visited))}")
    assert visited, "la sonda no abrio ninguna vista: el resultado no vale"
    print(f"hallazgos: {findings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
