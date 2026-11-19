"""Offline UI tool: measure text contrast across every visual style.

Not part of the bot. Polishing colours by eye hides the cases that fail in a
single palette, and reading the computed background is useless here because
most text sits on a gradient. So this screenshots the real app in all 14
styles and both themes and samples the rendered pixels behind each text node:
the background is the most common colour inside the element's box, the ink is
the computed text colour, and the WCAG ratio is taken between them.

Usage: PYTHONPATH=. python3 tools/contrast_probe.py [--min 4.5] [--tabs home,ai]
"""
from __future__ import annotations

import argparse
import io
import re
from collections import Counter
from pathlib import Path

from PIL import Image

from tests.test_webxdc_browser import browser_fixture

CORE = Path(__file__).resolve().parent.parent / "webxdc" / "core.js"

COLLECT = """
() => {
  const out = [];
  for (const el of document.querySelectorAll("body *")) {
    const text = [...el.childNodes]
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join(" ")
      .trim();
    if (!text) continue;
    const b = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (b.width < 6 || b.height < 6 || b.top < 0) continue;
    if (cs.visibility === "hidden" || cs.opacity === "0") continue;
    if (b.top > innerHeight || b.left > innerWidth) continue;
    const m = /rgba?\\(([^)]+)\\)/.exec(cs.color);
    if (!m) continue;
    const parts = m[1].split(/[\\s,/]+/).filter(Boolean).map(Number);
    if (parts.length > 3 && parts[3] < 0.95) continue;
    out.push({
      text: text.slice(0, 40), cls: (el.className || "").toString().slice(0, 28),
      ink: parts.slice(0, 3),
      x: Math.round(b.left), y: Math.round(b.top),
      w: Math.round(Math.min(b.width, innerWidth - b.left)),
      h: Math.round(Math.min(b.height, innerHeight - b.top)),
      size: Math.round(parseFloat(cs.fontSize)),
      bold: parseInt(cs.fontWeight, 10) >= 700,
    });
  }
  return out;
}
"""


def luminance(rgb) -> float:
    acc = []
    for value in rgb[:3]:
        s = value / 255
        acc.append(s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * acc[0] + 0.7152 * acc[1] + 0.0722 * acc[2]


def ratio(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def styles() -> list[str]:
    block = re.search(r"const VISUAL_STYLES = Object\.freeze\(\[(.*?)\]\)", CORE.read_text(), re.S)
    return re.findall(r'"([a-z0-9_-]+)"', block.group(1))


def background_of(shot: Image.Image, node: dict, scale: int) -> tuple | None:
    """Most common pixel inside the box, ignoring the glyphs themselves.

    Taking the plain mode is wrong on a gradient: antialiased strokes of the
    text can win and the ratio collapses towards 1. Pixels close to the ink
    colour are dropped first so the background is what remains.
    """
    box = (node["x"] * scale, node["y"] * scale,
           (node["x"] + node["w"]) * scale, (node["y"] + node["h"]) * scale)
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    crop = shot.crop(box)
    total = crop.width * crop.height
    if total < 24:
        return None
    ink = node["ink"]
    counts = Counter()
    for pixel in crop.getdata():
        distance = sum(abs(pixel[i] - ink[i]) for i in range(3))
        if distance > 150:
            counts[pixel[:3]] += 1
    if not counts:
        return None
    colour, hits = counts.most_common(1)[0]
    # an even spread means no flat background behind the text: not measurable
    if hits < total * 0.12:
        return None
    return colour


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min", type=float, default=4.5)
    parser.add_argument("--large-min", type=float, default=3.0)
    parser.add_argument("--tabs", default="home,ai,web,apps,downloads,settings")
    parser.add_argument("--language", default="es")
    parser.add_argument("--styles", default="")
    options = parser.parse_args()

    tabs = [t for t in options.tabs.split(",") if t]
    chosen = [s for s in options.styles.split(",") if s] or styles()
    hallazgos, medidos, saltados = [], 0, 0

    with browser_fixture(options.language, {"width": 390, "height": 844},
                         engine="chromium", touch=True) as (page, _errors):
        page.wait_for_function("cache.at > 0")
        for theme in ("light", "dark"):
            page.evaluate(f"document.documentElement.dataset.theme = {theme!r}")
            for style in chosen:
                page.evaluate(f"document.documentElement.dataset.style = {style!r}")
                for tab in tabs:
                    page.evaluate(f"goTab({tab!r})")
                    page.wait_for_timeout(200)
                    nodes = page.evaluate(COLLECT)
                    shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                    scale = max(1, round(shot.width / 390))
                    for node in nodes:
                        bg = background_of(shot, node, scale)
                        if bg is None:
                            saltados += 1
                            continue
                        medidos += 1
                        value = ratio(node["ink"], bg)
                        large = node["size"] >= 24 or (node["size"] >= 19 and node["bold"])
                        umbral = options.large_min if large else options.min
                        if value < umbral:
                            hallazgos.append((round(value, 2), style, theme, tab,
                                              node["text"], node["cls"], node["size"], umbral))

    print(f"\ntextos medidos: {medidos}  ·  sin fondo plano (no medibles): {saltados}")
    print(f"por debajo del umbral ({options.min} normal / {options.large_min} grande): {len(hallazgos)}\n")
    vistos = set()
    for value, style, theme, tab, text, cls, size, umbral in sorted(hallazgos):
        clave = (text, cls, theme)
        if clave in vistos:
            continue
        vistos.add(clave)
        print(f"  {value:5.2f} (< {umbral})  {style}/{theme}/{tab:9} {size:3}px .{cls:28} {text!r}")
    if len(vistos) < len(hallazgos):
        print(f"\n  (+{len(hallazgos) - len(vistos)} repeticiones del mismo texto en otros estilos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
