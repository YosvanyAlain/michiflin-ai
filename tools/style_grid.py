"""Offline UI tool: render the same screen in all 14 visual styles.

Not part of the bot. The style selector lives on :root, so a style can only be
judged next to the others. This drives the real app through the same fake Delta
bridge the browser tests use, switches data-style, and stitches the captures
into one strip per theme so incoherence is visible instead of imagined.

Usage: PYTHONPATH=. python3 tools/style_grid.py --tab home --theme light
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw

from tests.test_webxdc_browser import browser_fixture

CORE = Path(__file__).resolve().parent.parent / "webxdc" / "core.js"


def styles() -> list[str]:
    block = re.search(r"const VISUAL_STYLES = Object\.freeze\(\[(.*?)\]\)", CORE.read_text(), re.S)
    assert block, "VISUAL_STYLES no encontrado en core.js"
    return re.findall(r'"([a-z0-9_-]+)"', block.group(1))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/home/user/estilos.png")
    parser.add_argument("--tab", default="home")
    parser.add_argument("--theme", default="light")
    parser.add_argument("--language", default="es")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--columns", type=int, default=7)
    options = parser.parse_args()

    names = styles()
    shots: list[tuple[str, Image.Image]] = []
    tmp = Path("/tmp/style_grid")
    tmp.mkdir(parents=True, exist_ok=True)

    with browser_fixture(
        options.language, {"width": options.width, "height": options.height},
        engine="chromium", touch=True,
    ) as (page, _errors):
        page.wait_for_function("cache.at > 0")
        page.evaluate(f"document.documentElement.dataset.theme = {options.theme!r}")
        page.evaluate(f"goTab({options.tab!r})")
        page.wait_for_timeout(300)
        for name in names:
            page.evaluate(f"document.documentElement.dataset.style = {name!r}")
            page.wait_for_timeout(420)
            path = tmp / f"{name}.png"
            page.screenshot(path=str(path))
            shots.append((name, Image.open(path).convert("RGB")))

    cols = options.columns
    rows = (len(shots) + cols - 1) // cols
    w, h = shots[0][1].size
    pad, label = 14, 26
    sheet = Image.new("RGB", (cols * (w + pad) + pad, rows * (h + pad + label) + pad), "#15161b")
    draw = ImageDraw.Draw(sheet)
    for i, (name, img) in enumerate(shots):
        x = pad + (i % cols) * (w + pad)
        y = pad + (i // cols) * (h + pad + label)
        sheet.paste(img, (x, y))
        draw.text((x + 4, y + h + 6), f"{name} · {options.theme}", fill="#d6dae5")
    sheet.save(options.out)
    print(f"{len(shots)} estilos · {options.tab} · {options.theme} -> {options.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
