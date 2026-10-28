"""Headless Chromium worker for full-page captures, printable PDFs and script-heavy pages.

Playwright's sync API is bound to one thread, so a single daemon thread owns the
browser and executes submitted tasks in order.
"""
from __future__ import annotations

import io
import os
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from . import chat, config, net

log = logging.getLogger(__name__)

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#didomi-notice-agree-button",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    ".qc-cmp2-summary-buttons button[mode='primary']",
    "button[data-testid='uc-accept-all-button']",
    "#truste-consent-button",
    ".trustarc-agree-btn",
    "button.sp_choice_type_11",
    "button[title='Accept']",
    "button[aria-label='Accept all']",
    "button[aria-label='Aceptar todo']",
    "form[action*='consent'] button",
    ".fc-cta-consent",
    ".cmp-accept",
    "#accept-cookies",
    "#cookie-accept",
    ".cookie-accept",
    ".js-accept-cookies",
]
CONSENT_TEXT = r"/^(aceptar( todo| todas| y cerrar| cookies)?|acepto|estoy de acuerdo|entendido|permitir( todo| todas)?|accept( all| cookies| & close)?|i accept|agree|i agree|allow( all)?|got it|ok|okay|consent|continuar)$/i"
CONSENT_SCRIPT = """
(pattern) => {
  const re = new RegExp(pattern.slice(1, pattern.lastIndexOf('/')), 'i');
  const nodes = document.querySelectorAll('button, [role=button], a.button, input[type=submit], input[type=button]');
  for (const node of nodes) {
    const text = (node.innerText || node.value || node.getAttribute('aria-label') || '').trim();
    if (text.length < 40 && re.test(text)) { node.click(); return true; }
  }
  return false;
}
"""
CLEANUP_SCRIPT = """
() => {
  for (const el of document.querySelectorAll('body *')) {
    const style = getComputedStyle(el);
    if ((style.position === 'fixed' || style.position === 'sticky') && el.offsetHeight > 0) {
      const text = (el.innerText || '').toLowerCase();
      if (/cookie|consent|privacidad|privacy|gdpr|suscr|subscribe|newsletter/.test(text) || el.offsetHeight > innerHeight * 0.3) {
        el.remove();
      } else if (style.position === 'fixed') {
        el.style.position = 'absolute';
      }
    }
  }
  document.documentElement.style.overflow = 'visible';
  document.body.style.overflow = 'visible';
}
"""
# The element that really scrolls: the page itself, or an inner box on app-like sites.
SCROLLER_JS = """
const scroller = () => {
  const root = document.scrollingElement || document.documentElement;
  if (root.scrollHeight > innerHeight + 50) return root;
  let best = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.clientHeight < innerHeight * 0.5 || el.scrollHeight < el.clientHeight + 200) continue;
    const overflow = getComputedStyle(el).overflowY;
    if ((overflow === 'auto' || overflow === 'scroll') && (!best || el.scrollHeight > best.scrollHeight)) best = el;
  }
  return best || root;
};
"""
# Content signature: stops changing once scripts have finished building the page.
SIGNATURE_SCRIPT = "() => { const s = document.scrollingElement || document.documentElement; return [document.body ? document.body.innerText.length : 0, document.getElementsByTagName('*').length, s.scrollHeight].join(':'); }"
SCROLL_SCRIPT = """
async (limit) => {
""" + SCROLLER_JS + """
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const box = scroller();
  const view = box === (document.scrollingElement || document.documentElement) ? innerHeight : box.clientHeight;
  let y = 0;
  for (let i = 0; i < 80; i++) {
    y += Math.round(view * 0.8);
    box.scrollTo(0, y);
    await sleep(320);
    if (y >= Math.min(box.scrollHeight, limit)) break;
  }
  await sleep(600);
  box.scrollTo(0, 0);
  await sleep(300);
}
"""
# Make the page capturable in one piece: unwrap inner scrollers, show scroll-reveal content,
# load lazy images, and pin viewport-sized blocks so a tall capture does not stretch them.
PREPARE_SCRIPT = """
async () => {
""" + SCROLLER_JS + """
  const box = scroller();
  const root = document.scrollingElement || document.documentElement;
  if (box !== root) {
    for (let el = box; el && el !== document; el = el.parentElement) {
      el.style.setProperty('height', 'auto', 'important');
      el.style.setProperty('max-height', 'none', 'important');
      el.style.setProperty('overflow', 'visible', 'important');
    }
  }
  const style = document.createElement('style');
  style.textContent = '*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;transition:none!important;scroll-behavior:auto!important}' +
    '[data-aos],[data-sr],[data-scroll],[data-animate],.aos-init,.wow,.reveal,.fade-in,.fadeIn,.animate__animated,.sr,.lazyload,.lazy{opacity:1!important;visibility:visible!important;transform:none!important}';
  document.head.appendChild(style);
  const hidden = /aos|reveal|fade|animat|wow|scroll|appear|inview|in-view|lazy|motion/i;
  for (const el of document.querySelectorAll('body *')) {
    const cls = typeof el.className === 'string' ? el.className : '';
    const attrs = el.getAttributeNames().join(' ');
    if (!hidden.test(cls + ' ' + attrs)) continue;
    const cs = getComputedStyle(el);
    if (cs.opacity === '0' || cs.visibility === 'hidden') {
      el.style.setProperty('opacity', '1', 'important');
      el.style.setProperty('visibility', 'visible', 'important');
      el.style.setProperty('transform', 'none', 'important');
    }
  }
  for (const img of document.querySelectorAll('img')) {
    img.loading = 'eager';
    const lazy = img.dataset.src || img.dataset.lazySrc || img.dataset.original || img.dataset.url;
    if (lazy && (!img.getAttribute('src') || img.src.startsWith('data:'))) img.src = lazy;
    const set = img.dataset.srcset || img.dataset.lazySrcset;
    if (set && !img.getAttribute('srcset')) img.srcset = set;
  }
  for (const source of document.querySelectorAll('source[data-srcset]')) if (!source.srcset) source.srcset = source.dataset.srcset;
  for (const el of document.querySelectorAll('body *')) {
    const rect = el.getBoundingClientRect();
    if (rect.height >= innerHeight * 0.9 && rect.height <= innerHeight * 1.1) {
      const cs = getComputedStyle(el);
      if (/vh|svh|dvh|lvh/.test(el.getAttribute('style') || '') || cs.minHeight !== '0px' || cs.height === innerHeight + 'px') {
        el.style.setProperty('height', rect.height + 'px', 'important');
        el.style.setProperty('min-height', '0', 'important');
      }
    }
  }
  const pending = [...document.images].filter((img) => !img.complete);
  await Promise.race([
    Promise.all(pending.map((img) => new Promise((r) => { img.addEventListener('load', r, { once: true }); img.addEventListener('error', r, { once: true }); }))),
    new Promise((r) => setTimeout(r, 6000)),
  ]);
}
"""
MAX_CAPTURE_HEIGHT = 30000
SEGMENT_HEIGHT = 8000

_tasks: "queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], Future, str]]" = queue.Queue()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False


def _run_for_sender(function: Callable[..., Any], browser: Any, args: tuple[Any, ...], sender: str) -> Any:
    """Run one browser task with its own cookie identity, even on the shared browser thread."""
    previous = chat.sender()
    chat.set_sender(sender)
    try:
        return function(browser, *args)
    finally:
        chat.set_sender(previous)


def _worker() -> None:
    from playwright.sync_api import sync_playwright

    playwright = None
    browser = None
    while True:
        function, args, future, sender = _tasks.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            if browser is None or not browser.is_connected():
                if playwright is None:
                    playwright = sync_playwright().start()
                # Do not inherit API keys, backup credentials, or mail secrets into
                # the browser process. --no-sandbox remains necessary on some CI
                # runners; the request firewall below is a separate defense.
                safe_env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "FONTCONFIG_PATH", "FONTCONFIG_FILE", "LD_LIBRARY_PATH") if key in os.environ}
                browser = playwright.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"], env=safe_env)
            future.set_result(_run_for_sender(function, browser, args, sender))
        except Exception as exc:
            future.set_exception(exc)
            try:
                if browser is not None and not browser.is_connected():
                    browser = None
            except Exception:
                browser = None


def _submit(function: Callable[..., Any], *args: Any, timeout: int = 180) -> Any:
    global _thread
    with _thread_lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="browser", daemon=True)
            _thread.start()
    future: Future = Future()
    _tasks.put((function, args, future, chat.sender()))
    return future.result(timeout=timeout)


def _shrink(data: bytes, max_width: int = 900, quality: int = 55) -> bytes | None:
    """Re-encode a page image as a modest JPEG; PDFs store JPEG as-is but inflate other formats."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        original = image.format
        if getattr(image, "is_animated", False) or (original == "JPEG" and len(data) < 20_000):
            return None
        if image.width > max_width:
            image = image.resize((max_width, max(1, image.height * max_width // image.width)))
        output = io.BytesIO()
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.split()[-1])
            image = background
        image.convert("RGB").save(output, "JPEG", quality=quality, optimize=True, progressive=True)
        result = output.getvalue()
        if original == "JPEG" and len(result) >= len(data) * 0.9:
            return None
        return result
    except Exception:
        return None


def _context(browser: Any, device: str, block_heavy: bool = False, proxy: str = "", shrink_images: bool = False, scale: float = 1.0) -> Any:
    extra = {"proxy": {"server": proxy}} if proxy else {}
    if device == "desktop":
        context = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=net.DESKTOP_UA, locale="es-ES",
                                      device_scale_factor=scale, reduced_motion="reduce", **extra)
    else:
        context = browser.new_context(
            viewport={"width": 412, "height": 915}, user_agent=net.BROWSER_UA, is_mobile=True,
            has_touch=True, device_scale_factor=scale, locale="es-ES", reduced_motion="reduce", **extra,
        )
    cookies = []
    for cookie in net.cookie_jar():
        if cookie.domain:
            cookies.append({"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path or "/"})
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception:
            pass

    def route(route_request: Any) -> None:
        request = route_request.request
        scheme = request.url.split(":", 1)[0].lower() if ":" in request.url else ""
        if scheme in {"http", "https"}:
            try:
                net.validate_public_url(request.url)
            except ValueError:
                route_request.abort()
                return
        elif scheme not in {"data", "blob", "about"}:
            # Never allow file:, ftp:, or other browser-side schemes.
            route_request.abort()
            return
        if block_heavy and request.resource_type in {"image", "media", "font"}:
            route_request.abort()
            return
        if shrink_images and request.resource_type == "image":
            try:
                # Do not let Playwright's fetch helper follow redirects outside the
                # request firewall; the browser will follow them through this route.
                response = route_request.fetch(max_redirects=0)
                if 300 <= response.status < 400 and response.headers.get("location"):
                    route_request.continue_()
                    return
                body = response.body()
                smaller = _shrink(body) if len(body) > 2_000 else None
                if smaller:
                    headers = {**response.headers, "content-type": "image/jpeg", "content-length": str(len(smaller))}
                    route_request.fulfill(status=response.status, headers=headers, body=smaller)
                else:
                    route_request.fulfill(response=response)
            except Exception:
                route_request.continue_()
            return
        route_request.continue_()

    context.route("**/*", route)
    # WebSockets bypass the ordinary HTTP route interception. These page
    # previews do not need them, so close them rather than permit internal ws://.
    context.route_web_socket("**/*", lambda websocket: websocket.close())
    context.set_default_timeout(30000)
    return context


def _accept_consent(page: Any) -> None:
    for frame in [page.main_frame, *page.frames]:
        try:
            for selector in CONSENT_SELECTORS:
                element = frame.query_selector(selector)
                if element and element.is_visible():
                    element.click(timeout=2000)
                    page.wait_for_timeout(500)
                    return
            if frame.evaluate(CONSENT_SCRIPT, CONSENT_TEXT):
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _wait_stable(page: Any, limit: float = 10.0) -> None:
    """Wait until scripts stop building the page (same content three checks in a row)."""
    last, same, waited = None, 0, 0.0
    while waited < limit and same < 3:
        try:
            current = page.evaluate(SIGNATURE_SCRIPT)
        except Exception:
            return
        same = same + 1 if current == last else 0
        last = current
        page.wait_for_timeout(400)
        waited += 0.4


def _open(page: Any, url: str, settle: bool = True) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    _accept_consent(page)
    if not settle:
        return
    _wait_stable(page)
    for _ in range(2):
        try:
            page.evaluate(SCROLL_SCRIPT, MAX_CAPTURE_HEIGHT)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        before = page.evaluate(SIGNATURE_SCRIPT)
        _wait_stable(page, 4.0)
        if page.evaluate(SIGNATURE_SCRIPT) == before:
            break
    _accept_consent(page)
    for script in (CLEANUP_SCRIPT, PREPARE_SCRIPT):
        try:
            page.evaluate(script)
        except Exception:
            pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def network_error(exc: BaseException) -> bool:
    text = str(exc)
    return "Timeout" in text or "net::ERR_" in text


def _with_proxy_fallback(work: Callable[..., Any]) -> Callable[..., Any]:
    """Retry through WEB_PROXY when a site does not answer from the server's own address."""
    def run(browser: Any, *args: Any) -> Any:
        try:
            return work(browser, *args, proxy="")
        except Exception as exc:
            if not (config.WEB_PROXY and network_error(exc)):
                raise
            log.info("browser retry via proxy: %s", str(exc).splitlines()[0][:160])
            return work(browser, *args, proxy=config.WEB_PROXY)
    return run


def _render(browser: Any, url: str, proxy: str = "") -> tuple[str, str]:
    context = _context(browser, "desktop", block_heavy=True, proxy=proxy)
    try:
        page = context.new_page()
        _open(page, url, settle=False)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return page.url, page.content()
    finally:
        context.close()


# Capture level -> (device pixel ratio for mobile, for desktop, JPEG quality).
CAPTURE_LEVELS = {"normal": (1.0, 1.0, 70), "high": (2.0, 1.5, 82), "max": (3.0, 2.0, 90)}


def _capture(browser: Any, url: str, device: str, level: str, proxy: str = "") -> tuple[str, str, list[bytes], int]:
    mobile_scale, desktop_scale, quality = CAPTURE_LEVELS.get(level, CAPTURE_LEVELS["high"])
    scale = desktop_scale if device == "desktop" else mobile_scale
    context = _context(browser, device, proxy=proxy, scale=scale)
    try:
        page = context.new_page()
        _open(page, url)
        title = page.title()
        width = page.viewport_size["width"]
        height = min(int(page.evaluate("Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)") or 0), MAX_CAPTURE_HEIGHT) or 900
        images: list[bytes] = []
        top = 0
        step = max(1000, int(SEGMENT_HEIGHT / scale))
        while top < height:
            segment = min(step, height - top)
            images.append(page.screenshot(type="jpeg", quality=quality, full_page=True,
                                          clip={"x": 0, "y": top, "width": width, "height": segment}))
            top += segment
        return page.url, title, images, height
    finally:
        context.close()


def _pdf(browser: Any, url: str, proxy: str = "") -> tuple[str, str, bytes]:
    context = _context(browser, "desktop", proxy=proxy, shrink_images=True)
    try:
        page = context.new_page()
        _open(page, url)
        page.emulate_media(media="screen")
        title = page.title()
        data = page.pdf(format="A4", print_background=True, margin={"top": "8mm", "bottom": "8mm", "left": "6mm", "right": "6mm"})
        return page.url, title, data
    finally:
        context.close()


def render(url: str) -> tuple[str, str]:
    """Return (final_url, html) after running page scripts."""
    return _submit(_with_proxy_fallback(_render), url, timeout=150)


def capture(url: str, device: str = "mobile", level: str = "high") -> tuple[str, str, list[bytes], int]:
    """Full-page JPEG capture split into tall segments: (final_url, title, images, height)."""
    return _submit(_with_proxy_fallback(_capture), url, device, level, timeout=240)


def pdf(url: str) -> tuple[str, str, bytes]:
    """Printable A4 PDF with selectable text, working links and images."""
    return _submit(_with_proxy_fallback(_pdf), url, timeout=240)


def join_vertical(images: list[bytes], quality: int = 62) -> list[bytes]:
    """Re-encode segments smaller when possible; kept separate so each stays readable."""
    try:
        from PIL import Image
    except ImportError:
        return images
    output = []
    for data in images:
        with Image.open(io.BytesIO(data)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
            output.append(buffer.getvalue() if buffer.tell() < len(data) else data)
    return output


def warm_up() -> None:
    """Start Chromium early so the first request is fast."""
    try:
        _submit(lambda browser: browser.version, timeout=60)
    except Exception:
        pass


__all__ = ["available", "capture", "pdf", "render", "warm_up", "join_vertical", "time"]
