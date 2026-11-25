"""Compact reader view: a page reduced to readable text and links, paginated by size."""
from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from . import browser, chat, links, names, net, state
from .config import MAX_PAGE_BYTES
from .util import clean_text, compact, host_of, unlinked

JUNK_TAGS = ["script", "style", "noscript", "iframe", "svg", "canvas", "form", "button", "input", "select",
             "textarea", "template", "object", "embed", "dialog", "video", "audio", "link", "meta"]
JUNK_PATTERN = re.compile(
    r"(^|[-_ ])(cookie|cookies|consent|gdpr|cmp|popup|modal|overlay|newsletter|subscribe|suscri|advert|ads|ad|adv|"
    r"sponsor|sponsored|promo|banner|share|sharing|social|related|recommend|outbrain|taboola|paywall|signin|login|"
    r"breadcrumb|skip|sr-only|visually-hidden)([-_ ]|$)",
    re.I,
)
ALLOWED = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a", "b", "strong", "i", "em", "blockquote",
           "pre", "code", "table", "thead", "tbody", "tr", "td", "th", "br", "hr", "img", "figure", "figcaption",
           "dl", "dt", "dd", "sup", "sub", "small", "mark", "s", "u", "q", "cite", "abbr", "time", "caption"}
BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "table", "figure", "dl", "hr"}
STYLE = (
    "body{font:16px/1.7 system-ui,sans-serif;margin:0 auto;max-width:720px;padding:16px;color:#222;overflow-wrap:anywhere}"
    "h1,h2,h3{line-height:1.3}p,ul,ol{margin:0 0 1em}figure{margin:1.25em 0}"
    "img{display:block;max-width:100%;height:auto;border-radius:8px}figcaption{color:#555;font-size:.9em;margin-top:.4em}"
    "pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f4f4;padding:10px;border-radius:6px}"
    "table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;font-size:14px}"
    "td,th{border:1px solid #ccc;padding:5px}a{color:#0a58ca}.m{color:#555;font-size:13px}"
    ".nav a{display:inline-block;margin:3px 8px 3px 0}"
    "blockquote{border-left:3px solid #aaa;margin:1em 0;padding-left:14px;color:#444}"
)
CACHE_SECONDS = 3 * 3600
CACHE_ENTRIES = 64
CACHE_BYTES = 24 * 1024 * 1024
IMAGE_ATTEMPTS = 4
IMAGE_INPUT_BYTES = 1024 * 1024
IMAGE_PIXELS = 8_000_000
IMAGE_OUTPUT_BYTES = 12_000
MAX_LINK_CHARS = 4096
SITE_PAUSE_SECONDS = 120
_image_slots = threading.BoundedSemaphore(2)
_site_pause_lock = threading.Lock()
_site_pauses: dict[str, float] = {}


@dataclass
class Page:
    url: str
    title: str = ""
    description: str = ""
    blocks: list[str] = field(default_factory=list)
    text: str = ""
    nav: list[tuple[str, str]] = field(default_factory=list)
    created: float = field(default_factory=time.time)


class NotHtml(Exception):
    def __init__(self, content_type: str):
        super().__init__(content_type)
        self.content_type = content_type


class _ImageBlocked(Exception):
    pass


def _pause_site(url: str) -> None:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return
    if not host:
        return
    with _site_pause_lock:
        now = time.monotonic()
        for old in [name for name, until in _site_pauses.items() if until <= now]:
            _site_pauses.pop(old, None)
        _site_pauses.pop(host, None)
        _site_pauses[host] = now + SITE_PAUSE_SECONDS
        while len(_site_pauses) > 128:
            _site_pauses.pop(next(iter(_site_pauses)))


def _site_paused(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    with _site_pause_lock:
        until = _site_pauses.get(host, 0.0)
        if until <= time.monotonic():
            _site_pauses.pop(host, None)
            return False
        return True


# Pages may contain cookie-dependent private content. Never share them merely
# because another contact asks for the same URL. Entries are process-local.
CacheKey = tuple[str, str, str, str, bytes, bytes]
_cache: dict[CacheKey, Page] = {}
_cache_lock = threading.RLock()
_cache_versions: dict[str, tuple[bytes, bytes]] = {}
_cookie_digest_key = secrets.token_bytes(32)


def _href(url: str) -> str:
    return links.mailto(url) if state.setting("reader_links") == "chat" else url


def _url(base: str, value: object) -> str:
    """Only return a bounded HTTP(S) URL; actual fetches also use the egress firewall."""
    if (not isinstance(value, str) or not value.strip() or value.strip().startswith("#")
            or len(value) > MAX_LINK_CHARS or any(ord(char) < 32 for char in value)):
        return ""
    try:
        absolute = urljoin(base, value.strip())
        parsed = urlparse(absolute)
        if (len(absolute) > MAX_LINK_CHARS or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username is not None or parsed.password is not None):
            return ""
        return absolute
    except ValueError:
        return ""


def _srcset(value: object) -> list[str]:
    if not isinstance(value, str) or len(value) > 16000 or re.search(r"(?:^|,)\s*data:", value, re.I):
        return []
    # Ignore descriptors; site-provided image bytes never reach the UI.
    return [piece.strip().split()[0] for piece in value.split(",") if piece.strip()]


class _ChallengeProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.suppressed = 0
        self.in_title = False
        self.title: list[str] = []
        self.text: list[str] = []
        self.size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self.suppressed += 1
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"} and self.suppressed:
            self.suppressed -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.suppressed:
            return
        if self.in_title and len(self.title) < 4:
            self.title.append(data[:250])
        if self.size < 2500:
            part = data[:2500 - self.size]
            self.text.append(part)
            self.size += len(part)


_CHALLENGE_TEXT = re.compile(
    r"checking your browser|checking if the site connection is secure|verify (?:that )?you are (?:a )?human|"
    r"prove (?:that )?you are (?:a )?human|verify (?:that )?you are not a robot|"
    r"unusual traffic from your (?:computer|network)|verifica que no eres (?:un )?robot|"
    r"confirma que no eres (?:un )?robot|comprobando tu navegador", re.I,
)


def _challenge_markup(markup: str) -> bool:
    # Inspect only a bounded prefix, before parsing or requesting thumbnails.
    try:
        probe = _ChallengeProbe()
        probe.feed(markup[:200_000])
        probe.close()
        title = " ".join(probe.title).strip().casefold().rstrip(". !")
        return bool(title in {"just a moment", "attention required", "captcha", "verificación de seguridad"}
                    or _CHALLENGE_TEXT.search(" ".join(probe.text)))
    except Exception:
        return False


def _tiny_image(source: str) -> str:
    """Fetch one public image without stored cookies; return a bounded JPEG only."""
    if _site_paused(source):
        raise _ImageBlocked()
    if not _image_slots.acquire(timeout=0.5):
        return ""
    try:
        # Sites can name third-party images. Never forward the contact's saved
        # cookies or credentials to those origins, including across redirects.
        response = net.fetch(source, stream=True, timeout=(8, 15), anonymous=True, retry_blocked=False)
        try:
            if response.status_code in {401, 403, 429, 451, 503}:
                if response.status_code == 429:
                    _pause_site(source)
                    _pause_site(response.url)
                raise _ImageBlocked()
            if not response.ok or not response.headers.get("content-type", "").lower().startswith("image/"):
                return ""
            data = net.read_limited(response, IMAGE_INPUT_BYTES)
        finally:
            response.close()
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if (image.width < 60 or image.height < 60 or image.width * image.height > IMAGE_PIXELS):
                return ""
            image.thumbnail((320, 320))
            rgb = image.convert("RGB")
        for side, quality in ((320, 45), (320, 30), (240, 30)):
            if side < 320:
                rgb.thumbnail((side, side))
            buffer = io.BytesIO()
            rgb.save(buffer, "JPEG", quality=quality, optimize=True)
            raw = buffer.getvalue()
            if len(raw) <= IMAGE_OUTPUT_BYTES:
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        return ""
    except _ImageBlocked:
        raise
    except Exception:
        return ""
    finally:
        _image_slots.release()


def _remove_junk(soup) -> None:
    # Retain an ordinary <img> fallback inside <picture>, or remember a source
    # candidate if it only has a data-URI placeholder. <source> itself is not
    # sent to the client and never causes a network request there.
    for picture in soup.find_all("picture"):
        image = picture.find("img")
        if image:
            candidates = [item for source in picture.find_all("source")
                          for item in _srcset(source.get("srcset") or source.get("data-srcset"))]
            if candidates:
                image["data-reader-picture"] = candidates[0]
    # lxml's HTML parser can incorrectly nest the fallback <img> in <source>.
    # Unwrap it instead of decomposing it, while dropping the source tag itself.
    for source in soup.find_all("source"):
        source.unwrap()
    for name in JUNK_TAGS:
        for node in soup.find_all(name):
            node.decompose()
    for node in soup.find_all(True):
        if getattr(node, "decomposed", False) or node.name in {"html", "body", "main", "article"}:
            continue
        attrs = getattr(node, "attrs", None) or {}
        marker = " ".join([str(attrs.get("id", ""))] + [str(item) for item in attrs.get("class", []) or []])
        role = str(attrs.get("role", "")).lower()
        style = str(attrs.get("style", "")).replace(" ", "").lower()
        hidden = "display:none" in style or "visibility:hidden" in style or attrs.get("aria-hidden") == "true" or "hidden" in attrs
        junk = role in {"dialog", "alertdialog", "banner", "navigation", "complementary"} or (marker.strip() and JUNK_PATTERN.search(marker))
        if hidden or (junk and len(node.get_text(" ", strip=True)) < 1500):
            node.decompose()
    for node in soup.find_all(["header", "footer", "nav", "aside"]):
        if getattr(node, "decomposed", False):
            continue
        # Author and date inside an article are content, not the site's menu.
        if node.name in {"header", "footer"} and node.find_parent(["article", "main"]):
            node.unwrap()
        else:
            node.decompose()


def _clean(soup, base: str, images: bool, image_budget: list[int]) -> None:
    """Sanitize the chosen content only; no page script, CSS or remote image survives."""
    for node in soup.find_all(True):
        attrs = getattr(node, "attrs", None)
        if attrs is None:
            continue
        if node.name == "a":
            absolute = _url(base, attrs.get("href"))
            node.attrs = {"href": _href(absolute)} if absolute else {}
            continue
        if node.name == "img":
            candidates = [attrs.get(key) for key in ("data-src", "data-original", "data-lazy-src", "src")]
            candidates += _srcset(attrs.get("srcset")) + _srcset(attrs.get("data-srcset"))
            candidates.append(attrs.get("data-reader-picture"))
            absolute = next((result for value in candidates if (result := _url(base, value))), "")
            alt = compact(attrs.get("alt", ""), 120)
            node.attrs = {}
            if images and absolute and image_budget[0] > 0:
                # The budget counts attempts, even failed or tiny images.
                image_budget[0] -= 1
                try:
                    inline = _tiny_image(absolute)
                except _ImageBlocked:
                    image_budget[0] = 0  # stop further thumbnail requests after a restriction
                    inline = ""
                if inline:
                    node.attrs = {"src": inline, "alt": alt}
                    continue
            if alt:
                node.replace_with(f"[{alt}]")
            else:
                node.decompose()
            continue
        if node.name in {"td", "th"}:
            span = str(attrs.get("colspan") or "")
            node.attrs = {"colspan": span} if span.isdigit() and 1 <= int(span) <= 100 else {}
            continue
        node.attrs = {}


def _main(soup):
    for selector in ["article", "main", "[role=main]", "#content", "#main", ".post-content", ".entry-content", ".article-body", ".content"]:
        found = soup.select(selector)
        if found:
            best = max(found, key=lambda node: len(node.get_text(" ", strip=True)))
            if len(best.get_text(" ", strip=True)) > 400:
                return best
    return soup.body or soup


def _render_node(node) -> str:
    """Serialize only allowed tags; unknown containers are unwrapped."""
    from bs4 import NavigableString, Tag

    if isinstance(node, NavigableString):
        return html.escape(str(node), quote=False)
    if not isinstance(node, Tag):
        return ""
    inner = "".join(_render_node(child) for child in node.children)
    name = node.name
    if name not in ALLOWED:
        return inner + ("\n" if name in {"div", "section", "article", "main"} else "")
    if name in {"br", "hr"}:
        return f"<{name}>"
    if name == "img":
        return f'<img src="{node.attrs.get("src", "")}" alt="{html.escape(node.attrs.get("alt", ""))}">'
    if not inner.strip() and name not in {"td", "th"}:
        return ""
    attrs = "".join(f' {key}="{html.escape(str(value))}"' for key, value in node.attrs.items())
    return f"<{name}{attrs}>{inner}</{name}>"


def _split_blocks(markup: str) -> list[str]:
    """Split only at top-level boundaries, never through a figure, list or table."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    root = BeautifulSoup(markup, "html.parser")
    blocks: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        part = "".join(pending).strip()
        pending.clear()
        if part and (re.sub(r"<[^>]+>", "", part).strip() or re.search(r"<img\b", part, re.I)):
            blocks.append("<p>" + re.sub(r"\n+", "<br>", part) + "</p>")

    for node in root.contents:
        if isinstance(node, Tag) and node.name in BLOCKS:
            flush()
            blocks.append(str(node))
        elif isinstance(node, NavigableString):
            pending.append(html.escape(str(node), quote=False))
        elif isinstance(node, Tag):
            pending.append(str(node))
    flush()
    return blocks


def _nav_links(soup, base: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    base_host = host_of(base)
    for anchor in soup.find_all("a", href=True):
        target = _url(base, anchor["href"]).split("#")[0]
        label = compact(anchor.get_text(" ", strip=True), 60)
        if not target or target in seen or len(label) < 3 or target == base:
            continue
        if host_of(target) != base_host:
            continue
        seen.add(target)
        found.append((label, target))
        if len(found) >= 30:
            break
    return found


def decode_page(data: bytes, content_type: str) -> str:
    """Declared charset first (header, then meta tag), otherwise UTF-8, otherwise Windows-1252."""
    declared = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    if declared:
        name = declared.group(1)
        try:
            return data.decode(name.decode() if isinstance(name, bytes) else name, errors="replace")
        except LookupError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse(url: str, markup: str, image_budget: list[int] | None = None) -> Page:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "", 200)
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = compact(meta.get("content", "") if meta else "", 300)
    nav_source = BeautifulSoup(markup, "lxml")
    nav = _nav_links(nav_source.find("nav") or nav_source.find("header") or nav_source, url)
    images = state.setting("reader_images") == "low"
    _remove_junk(soup)
    main = _main(soup)
    # Do not fetch tracking images from menus or unrelated page sections. A
    # browser fallback shares the same attempt budget as the first fetch.
    _clean(main, url, images, image_budget if image_budget is not None else [IMAGE_ATTEMPTS])
    heading = main.find("h1")
    if (title and heading and compact(heading.get_text(" ", strip=True), 200).casefold() == title.casefold()
            and not heading.find(["a", "img"])):
        remaining = compact(main.get_text(" ", strip=True), 500)
        if remaining.casefold() != title.casefold() or main.find("img"):
            # Both the chat document and the current app already add page.title.
            # Keep a title-only page readable rather than making it empty.
            heading.decompose()
    markup_out = _render_node(main)
    blocks = _split_blocks(markup_out)
    text = clean_text(main.get_text("\n", strip=True))
    text = re.sub(r"(?<=[(\[«¿¡])\n|\n(?=[.,;:!?)\]»%])", "", text)
    return Page(url=url, title=title or unlinked(host_of(url)), description=description, blocks=blocks, text=text, nav=nav)


def _cookie_stamp() -> bytes | None:
    """Digest this contact's cookie state, without retaining or logging its values."""
    try:
        identity, jar = net.cookie_context()
        with jar._cookies_lock:
            # A generation distinguishes recreated jars even if their cookies
            # are identical; a revision detects changes later restored during
            # a fetch. Unknown jar implementations fail closed.
            generation, revision = jar._generation, jar._revision
            if not isinstance(generation, bytes) or len(generation) != 16 or not isinstance(revision, int) or revision < 0:
                return None
            cookies = list(jar)
            if len(cookies) > 512:
                return None
            now = time.time()
            rows = [json.dumps([cookie.version, cookie.name, cookie.value, cookie.port, cookie.port_specified,
                                cookie.domain, cookie.domain_specified, cookie.domain_initial_dot, cookie.path,
                                cookie.path_specified, cookie.secure, cookie.expires, cookie.discard,
                                cookie.is_expired(now)], ensure_ascii=True, separators=(",", ":")) for cookie in cookies]
        digest = hashlib.blake2b(key=_cookie_digest_key, digest_size=16)
        identity_bytes = identity.encode("utf-8")
        digest.update(len(identity_bytes).to_bytes(8, "big"))
        digest.update(identity_bytes)
        digest.update(generation)
        revision_bytes = str(revision).encode("ascii")
        digest.update(len(revision_bytes).to_bytes(8, "big"))
        digest.update(revision_bytes)
        for row in sorted(rows):
            encoded = row.encode("ascii")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.digest()
    except Exception:
        return None  # unknown cookie state must never reuse a private page


def invalidate_sender(address: str) -> None:
    """Discard one contact's pages and make any in-flight cache insertion stale."""
    sender = str(address or "").strip().lower()
    if not sender:
        return
    with _cache_lock:
        _cache_versions.pop(sender, None)
        for old in [key for key in _cache if key[0] == sender]:
            _cache.pop(old, None)


def _cache_key(url: str) -> CacheKey | None:
    sender = chat.sender().strip().lower()
    # Unknown or conflicting thread contexts are not a security principal.
    if (not sender or sender != state.request_sender().strip().lower()
            or not isinstance(url, str) or len(url) > MAX_LINK_CHARS):
        return None
    stamp = _cookie_stamp()
    if stamp is None:
        return None
    with _cache_lock:
        previous = _cache_versions.pop(sender, None)
        if previous is None or previous[1] != stamp:
            if previous is not None:
                for old in [key for key in _cache if key[0] == sender]:
                    _cache.pop(old, None)
            version = secrets.token_bytes(16)
        else:
            version = previous[0]
        _cache_versions[sender] = (version, stamp)
        # Bound the number of principals; a recreated principal receives a new
        # random version, so an older in-flight fetch cannot reinsert its page.
        while len(_cache_versions) > 256:
            oldest = next(iter(_cache_versions))
            _cache_versions.pop(oldest, None)
            for old in [key for key in _cache if key[0] == oldest]:
                _cache.pop(old, None)
    return sender, url, str(state.setting("reader_images")), str(state.setting("reader_links")), stamp, version


def _cache_cost(page: Page) -> int:
    return (sys.getsizeof(page) + sys.getsizeof(page.url) + sys.getsizeof(page.title)
            + sys.getsizeof(page.description) + sys.getsizeof(page.text) + sys.getsizeof(page.blocks)
            + sum(sys.getsizeof(block) for block in page.blocks) + sys.getsizeof(page.nav)
            + sum(sys.getsizeof(label) + sys.getsizeof(target) for label, target in page.nav))


def _entry_cost(key: CacheKey, page: Page) -> int:
    return _cache_cost(page) + sys.getsizeof(key) + sum(sys.getsizeof(value) for value in key)


def _remember(page: Page, requested: str, key: CacheKey | None) -> None:
    if key is None or _entry_cost(key, page) > CACHE_BYTES:
        return
    # Cookie and settings reads may acquire state.lock. Do not perform them
    # while holding _cache_lock: saving a visual session takes those locks in
    # the opposite order when it invalidates this cache.
    if key != _cache_key(requested):
        return
    with _cache_lock:
        # A concurrent session invalidation rotates/removes the version under
        # this lock, so an older in-flight fetch cannot repopulate the cache.
        if _cache_versions.get(key[0]) != (key[5], key[4]):
            return
        aliases = [key]
        if len(page.url) <= MAX_LINK_CHARS:
            final_key = (key[0], page.url, *key[2:])
            if final_key != key:
                aliases.append(final_key)
        for old in [old for old, item in _cache.items() if time.time() - item.created >= CACHE_SECONDS]:
            _cache.pop(old, None)
        for alias in aliases:
            _cache.pop(alias, None)
            _cache[alias] = page
        # Count aliases conservatively; a page larger than the budget is never cached.
        used = sum(_entry_cost(item_key, item) for item_key, item in _cache.items())
        while _cache and (len(_cache) > CACHE_ENTRIES or used > CACHE_BYTES):
            old = next(iter(_cache))
            used -= _entry_cost(old, _cache.pop(old))


def load(url: str, force_browser: bool = False) -> Page:
    """Fetch and simplify a page; never retry detected challenges through another route."""
    if _site_paused(url):
        raise ValueError("rate_limited")
    key = _cache_key(url)
    final_url, markup = url, ""
    if not force_browser:
        response = net.fetch(url, stream=True, timeout=(15, 40), retry_blocked=False)
        try:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code == 429:
                _pause_site(url)
                _pause_site(response.url)
            if response.status_code >= 400:
                raise ValueError(f"http_{response.status_code}")
            if content_type and "html" not in content_type and "xml" not in content_type:
                raise NotHtml(content_type)
            data = net.read_limited(response, MAX_PAGE_BYTES)
            final_url = response.url
            markup = decode_page(data, response.headers.get("content-type", ""))
        finally:
            response.close()
        if _challenge_markup(markup):
            _pause_site(url)
            _pause_site(final_url)
            raise ValueError("challenge")
    image_budget = [IMAGE_ATTEMPTS]
    page = parse(final_url, markup, image_budget) if markup else Page(url=final_url)
    weak = len(page.text) < 500 or bool(re.search(r"enable javascript|habilita javascript", page.text[:2000], re.I))
    if weak and browser.available():
        try:
            rendered_url, rendered = browser.render(final_url)
            if _challenge_markup(rendered):
                _pause_site(url)
                _pause_site(rendered_url)
                raise ValueError("challenge")
            candidate = parse(rendered_url, rendered, image_budget)
            if len(candidate.text) > len(page.text):
                page = candidate
        except ValueError as exc:
            if str(exc) == "challenge":
                raise
        except Exception:
            pass
    if not page.text and not page.blocks:
        raise ValueError("empty_page")
    _remember(page, url, key)
    return page


def cached(url: str) -> Page | None:
    key = _cache_key(url)
    if key is None:
        return None
    with _cache_lock:
        if _cache_versions.get(key[0]) != (key[5], key[4]):
            return None
        page = _cache.get(key)
        if not page:
            return None
        if time.time() - page.created >= CACHE_SECONDS:
            _cache.pop(key, None)
            return None
        # Dictionary insertion order gives a bounded least-recently-used cache.
        _cache.pop(key)
        _cache[key] = page
        return page


class _BoundedHTML(HTMLParser):
    """Divide an oversized sanitized block into independent, balanced fragments."""

    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts: list[str] = []
        self.stack: list[tuple[str, str, str]] = []
        self.output: list[str] = []
        self.size = 0
        self.reserved = 0
        self.has_content = False

    @staticmethod
    def _size(value: str) -> int:
        return len(value.encode("utf-8"))

    def _write(self, value: str) -> None:
        self.output.append(value)
        self.size += self._size(value)

    def _flush(self) -> bool:
        if not self.has_content:
            return False
        fragment = "".join(self.output) + "".join(end for _tag, _start, end in reversed(self.stack))
        assert self._size(fragment) <= self.limit
        self.parts.append(fragment)
        self.output = [start for _tag, start, _end in self.stack if start]
        self.size = sum(self._size(start) for start in self.output)
        self.has_content = False
        return True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED:
            self.stack.append((tag, "", ""))
            return
        attributes = "".join(f' {key}="{html.escape(value or "", quote=True)}"' for key, value in attrs)
        start = f"<{tag}{attributes}>"
        if tag in {"img", "br", "hr"}:
            if self.size + self._size(start) + self.reserved > self.limit:
                self._flush()
            if self.size + self._size(start) + self.reserved <= self.limit:
                self._write(start)
                self.has_content = True
            elif tag == "img":
                alt = next((value for key, value in attrs if key == "alt"), "")
                if alt:
                    self.handle_data(f"[{alt}]")
            return
        end = f"</{tag}>"
        opening = self._size(start)
        closing = self._size(end)
        prefix = sum(self._size(item[1]) for item in self.stack)
        # Maliciously deep or long wrappers must leave room for actual content.
        if prefix + opening + self.reserved + closing > self.limit // 2:
            self.stack.append((tag, "", ""))
            return
        if self.size + opening + self.reserved + closing > self.limit:
            self._flush()
        self._write(start)
        self.stack.append((tag, start, end))
        self.reserved += closing

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br", "hr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                while len(self.stack) > position:
                    _name, _start, end = self.stack.pop()
                    self.reserved -= self._size(end)
                    self._write(end)
                break

    def handle_data(self, data: str) -> None:
        position = 0
        while position < len(data):
            room = self.limit - self.size - self.reserved
            if room < 6:
                if not self._flush():
                    raise ValueError("reader_fragment_has_no_room")
                continue
            candidate = data[position:position + room]
            if self._size(html.escape(candidate, quote=False)) <= room:
                count = len(candidate)
            else:
                low, high = 0, len(candidate)
                while low < high:
                    middle = (low + high + 1) // 2
                    if self._size(html.escape(candidate[:middle], quote=False)) <= room:
                        low = middle
                    else:
                        high = middle - 1
                count = low
            if not count:
                if not self._flush():
                    raise ValueError("reader_fragment_has_no_room")
                continue
            if position + count < len(data):
                space = max(candidate.rfind(" ", count // 2, count), candidate.rfind("\n", count // 2, count))
                if space >= count // 2:
                    count = space + 1
            self._write(html.escape(data[position:position + count], quote=False))
            self.has_content = True
            position += count
            if position < len(data):
                self._flush()

    def finish(self) -> list[str]:
        self.close()
        self._flush()
        return self.parts


def _bounded_blocks(block: str, limit: int) -> list[str]:
    if len(block.encode("utf-8")) <= limit:
        return [block]
    parser = _BoundedHTML(limit)
    parser.feed(block)
    return parser.finish()


def paginate(page: Page, limit_kb: int) -> list[list[str]]:
    limit = max(20, limit_kb) * 1024
    pages: list[list[str]] = [[]]
    size = 0
    for block in page.blocks:
        for fragment in _bounded_blocks(block, limit):
            encoded = len(fragment.encode("utf-8"))
            separator = int(bool(pages[-1]))  # the newline added by build/app_page
            if pages[-1] and size + separator + encoded > limit:
                pages.append([])
                size = 0
                separator = 0
            pages[-1].append(fragment)
            size += separator + encoded
    return pages


def build(page: Page, number: int, locale: str) -> tuple[str, str, int]:
    """Return (html, short_text, total_pages) for one reader page (1-based)."""
    from .util import tr

    chunks = paginate(page, int(state.setting("reader_kb")))
    total = len(chunks)
    number = max(1, min(number, total))
    body = "\n".join(chunks[number - 1])
    host = unlinked(host_of(page.url))
    actions = [
        f'<a href="{links.mailto(page.url, "pdf")}">PDF</a>',
        f'<a href="{links.mailto(page.url, names.name(locale, "capture"))}">{tr(locale, "Captura", "Screenshot")}</a>',
        f'<a href="{links.mailto(page.url, names.name(locale, "download"))}">{tr(locale, "Descargar", "Download")}</a>',
        f'<a href="{html.escape(page.url)}">{tr(locale, "Original", "Original")}</a>',
    ]
    nav = ""
    if number == 1 and page.nav:
        nav = '<p class="m nav">' + " ".join(f'<a href="{_href(target)}">{html.escape(label)}</a>' for label, target in page.nav[:20]) + "</p>"
    more = ""
    if number < total:
        more = f'<p><b><a href="{links.mailto(links.command(names.name(locale, "more"), page.url, str(number + 1)))}">{tr(locale, "Continuar", "Continue")} ({number + 1}/{total}) ›</a></b></p>'
    document = (
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(page.title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{html.escape(page.title)}</h1><p class='m'>{host}" + (f" · {number}/{total}" if total > 1 else "") + "</p>"
        f"{nav}{body}{more}<hr><p class='m'>{' · '.join(actions)}</p></body></html>"
    )
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = compact(html.unescape(plain), 420)
    plain = re.sub(r"\b([\w-]+)\.([a-z]{2,})\b", lambda match: unlinked(match.group(0)), plain)
    lines = [f"🌐 {page.title}", f"{host}" + (f" · {tr(locale, 'pág.', 'page')} {number}/{total}" if total > 1 else ""), "", plain + ("…" if plain else "")]
    commands = [links.command("pdf", page.url), links.command("cap", page.url)]
    if number < total:
        commands.insert(0, links.command(names.name(locale, "more"), page.url, str(number + 1)))
    lines += ["", " · ".join(commands)]
    return document, "\n".join(lines), total


APP_PAGE_KB = 70


def app_page(page: Page, number: int) -> dict:
    """One bounded reader page; an unusually large envelope may use status chunks."""
    chunks = paginate(page, min(int(state.setting("reader_kb")), APP_PAGE_KB))
    total = len(chunks)
    number = max(1, min(number, total))
    return {"url": page.url, "title": page.title, "host": host_of(page.url), "page": number, "pages": total,
            "html": "\n".join(chunks[number - 1]), "nav": [[label, target] for label, target in page.nav[:20]] if number == 1 else []}


def url_title(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rsplit("/", 1)[-1] or parsed.netloc
