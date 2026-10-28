"""Sandbox entrypoint for an interactive browser; invoked only by sandbox.py.

STDIN/STDOUT carry private, ephemeral messages over pipes. Never log inputs,
URLs, page content, cookies, passwords or provider errors.
"""
from __future__ import annotations

import base64
import json
import sys
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

WIDTH, HEIGHT = 480, 760
MAX_FRAME = 87_000
MAX_LINE = 3_000_000


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n")
    sys.stdout.flush()


def read() -> dict[str, Any] | None:
    raw = sys.stdin.buffer.readline(MAX_LINE + 1)
    if not raw or len(raw) > MAX_LINE:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError):
        return None


def valid_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                    and parsed.username is None and parsed.password is None and len(url) <= 4096)
    except ValueError:
        return False


def frame(page: Any) -> dict[str, Any]:
    page.wait_for_timeout(300)
    for quality in (60, 45, 30, 18):
        image = page.screenshot(type="jpeg", quality=quality, full_page=False, timeout=12_000,
                                animations="disabled", caret="hide", scale="css")
        if len(image) <= MAX_FRAME:
            return {"kind": "frame", "image": base64.b64encode(image).decode("ascii"),
                    "width": WIDTH, "height": HEIGHT}
    return {"kind": "error", "error": "frame_too_large"}


def main() -> int:
    initial = read()
    if not initial or not valid_url(str(initial.get("url") or "")):
        emit({"kind": "error", "error": "bad_url"})
        return 1
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            proxy={"server": "http://127.0.0.1:18080"},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--proxy-bypass-list=<-loopback>"],
        )
        try:
            options: dict[str, Any] = {"viewport": {"width": WIDTH, "height": HEIGHT}, "device_scale_factor": 2,
                                       "accept_downloads": False}
            if initial.get("storage") is not None:
                options["storage_state"] = initial["storage"]
            context = browser.new_context(**options)
            page = context.new_page()
            page.set_default_timeout(10_000)
            try:
                page.goto(str(initial["url"]), wait_until="domcontentloaded", timeout=35_000)
            except Exception:
                # The screenshot may still show a meaningful provider error or challenge.
                pass
            emit(frame(page))
            while True:
                request = read()
                if not request:
                    break
                action = str(request.get("action") or "")
                seq = request.get("seq")
                try:
                    pages = [item for item in context.pages if not item.is_closed()]
                    if pages:
                        page = pages[-1]  # provider login pop-ups are visible to the user
                    if action == "close":
                        emit({"kind": "closed", "seq": seq})
                        break
                    if action == "save":
                        storage = context.storage_state(indexed_db=True)
                        storage["_michiflin_user_agent"] = str(page.evaluate("navigator.userAgent"))[:512]
                        emit({"kind": "state", "seq": seq, "storage": storage})
                        continue
                    if action == "capture":
                        total = max(HEIGHT, int(page.evaluate("document.documentElement.scrollHeight || window.innerHeight")))
                        top = max(0, min(total - HEIGHT, int(page.evaluate("window.scrollY || 0"))))
                        height = max(HEIGHT, min(3000, total - top))
                        for quality in (60, 35):
                            data = page.screenshot(type="jpeg", quality=quality, full_page=True,
                                                   clip={"x": 0, "y": top, "width": WIDTH, "height": height},
                                                   timeout=15_000, animations="disabled", caret="hide", scale="device")
                            if len(data) <= 1_000_000:
                                break
                        else:
                            raise ValueError("capture_size")
                        emit({"kind": "capture", "seq": seq, "image": base64.b64encode(data).decode("ascii"), "height": height})
                        continue
                    if action == "page_url":
                        emit({"kind": "page_url", "seq": seq, "url": page.url})
                        continue
                    if action == "click":
                        x, y = float(request["x"]), float(request["y"])
                        if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
                            raise ValueError("coordinates")
                        page.mouse.click(x, y)
                    elif action == "text":
                        text = request.get("text")
                        if not isinstance(text, str) or len(text) > 4096:
                            raise ValueError("text")
                        page.keyboard.insert_text(text)
                    elif action == "key":
                        key = request.get("key")
                        if key not in {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp"}:
                            raise ValueError("key")
                        page.keyboard.press(key)
                    elif action == "scroll":
                        amount = int(request.get("amount", 0))
                        if amount not in {-600, 600}:
                            raise ValueError("scroll")
                        page.mouse.wheel(0, amount)
                    elif action == "back":
                        page.go_back(wait_until="domcontentloaded", timeout=15_000)
                    elif action == "reload":
                        page.reload(wait_until="domcontentloaded", timeout=15_000)
                    elif action == "goto":
                        url = str(request.get("url") or "")
                        if not valid_url(url):
                            raise ValueError("url")
                        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    elif action != "frame":
                        raise ValueError("action")
                    result = frame(page)
                    result["seq"] = seq
                    emit(result)
                except Exception:
                    # Do not report provider exception text: it can include tokens and URLs.
                    try:
                        result = frame(page)
                        result.update(seq=seq, warning="action_failed")
                        emit(result)
                    except Exception:
                        emit({"kind": "error", "seq": seq, "error": "action_failed"})
            context.close()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except Exception:
        # Do not let Python print a traceback containing private browser data.
        try:
            emit({"kind": "error", "error": "browser_failed"})
        except Exception:
            pass
        status = 1
    raise SystemExit(status)
