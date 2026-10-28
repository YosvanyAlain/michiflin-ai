"""Compact reader view: a page reduced to readable text and links, paginated by size."""
from __future__ import annotations

import base64
import html
import io
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from . import browser, links, names, net, state
from .config import MAX_PAGE_BYTES
from .util import clean_text, compact, host_of, unlinked

JUNK_TAGS = ["script", "style", "noscript", "iframe", "svg", "canvas", "form", "button", "input", "select",
             "textarea", "template", "object", "embed", "dialog", "video", "audio", "picture source", "link", "meta"]
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
    "body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:720px;padding:10px;color:#222}"
    "img{max-width:100%;height:auto}pre{white-space:pre-wrap;background:#f4f4f4;padding:6px}"
    "table{border-collapse:collapse;font-size:14px}td,th{border:1px solid #ccc;padding:3px}"
    "a{color:#0a58ca}.m{color:#666;font-size:13px}.nav a{display:inline-block;margin:3px 8px 3px 0}"
    "blockquote{border-left:3px solid #ccc;margin:0;padding-left:10px;color:#444}"
)
CACHE_SECONDS = 3 * 3600


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


_cache: dict[str, Page] = {}


def _href(url: str) -> str:
    return links.mailto(url) if state.setting("reader_links") == "chat" else url


def _tiny_image(source: str) -> str:
    """Download and shrink an image into a small inline JPEG data URI."""
    try:
        response = net.fetch(source, stream=True, timeout=(8, 15))
        if not response.ok or not response.headers.get("content-type", "").startswith("image/"):
            response.close()
            return ""
        data = net.read_limited(response, 4 * 1024 * 1024)
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if image.width < 60 or image.height < 60:
                return ""
            image.thumbnail((320, 320))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=45, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        return ""


def _clean(soup, base: str, images: bool, image_budget: list[int]) -> None:
    for name in JUNK_TAGS:
        for node in soup.find_all(name.split()[0]):
            node.decompose()
    for node in soup.find_all(["header", "footer", "nav", "aside"]):
        node.decompose()
    for node in soup.find_all(True):
        if getattr(node, "decomposed", False) or node.name in {"html", "body", "main", "article"}:
            continue
        attrs = getattr(node, "attrs", None) or {}
        marker = " ".join([str(attrs.get("id", ""))] + [str(item) for item in attrs.get("class", []) or []])
        role = str(attrs.get("role", ""))
        hidden = "display:none" in str(attrs.get("style", "")).replace(" ", "") or attrs.get("aria-hidden") == "true" or "hidden" in attrs
        junk = role in {"dialog", "alertdialog", "banner", "navigation", "complementary"} or (marker.strip() and JUNK_PATTERN.search(marker))
        if hidden or (junk and len(node.get_text(" ", strip=True)) < 1500):
            node.decompose()
    for node in soup.find_all(True):
        attrs = getattr(node, "attrs", None)
        if attrs is None:
            continue
        if node.name == "a":
            target = attrs.get("href", "")
            node.attrs = {}
            absolute = urljoin(base, target) if target else ""
            if absolute.startswith(("http://", "https://")):
                node.attrs["href"] = _href(absolute)
            continue
        if node.name == "img":
            source = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original") or ""
            alt = compact(attrs.get("alt", ""), 120)
            node.attrs = {}
            absolute = urljoin(base, source) if source and not source.startswith("data:") else ""
            if images and absolute and image_budget[0] > 0:
                inline = _tiny_image(absolute)
                if inline:
                    image_budget[0] -= 1
                    node.attrs = {"src": inline, "alt": alt}
                    continue
            if alt:
                node.replace_with(f"[{alt}]")
            else:
                node.decompose()
            continue
        if node.name in {"td", "th"} and attrs.get("colspan"):
            node.attrs = {"colspan": attrs["colspan"]}
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
    parts = re.split(r"(?=<(?:p|h[1-6]|ul|ol|blockquote|pre|table|figure|dl|hr)\b)", markup)
    blocks = []
    for part in parts:
        part = re.sub(r"\n{2,}", "\n", part).strip()
        if not part:
            continue
        if not re.match(r"<(p|h[1-6]|ul|ol|blockquote|pre|table|figure|dl|hr)\b", part):
            text_only = re.sub(r"<[^>]+>", "", part).strip()
            if len(text_only) < 2:
                continue
            part = "<p>" + part.replace("\n", "<br>") + "</p>"
        blocks.append(part)
    return blocks


def _nav_links(soup, base: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    base_host = host_of(base)
    for anchor in soup.find_all("a", href=True):
        target = urljoin(base, anchor["href"]).split("#")[0]
        label = compact(anchor.get_text(" ", strip=True), 60)
        if not target.startswith(("http://", "https://")) or target in seen or len(label) < 3 or target == base:
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


def parse(url: str, markup: str) -> Page:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "lxml")
    title = compact(soup.title.get_text(" ", strip=True) if soup.title else "", 200)
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = compact(meta.get("content", "") if meta else "", 300)
    nav_source = BeautifulSoup(markup, "lxml")
    nav = _nav_links(nav_source.find("nav") or nav_source.find("header") or nav_source, url)
    images = state.setting("reader_images") == "low"
    _clean(soup, url, images, [12])
    main = _main(soup)
    markup_out = _render_node(main)
    blocks = _split_blocks(markup_out)
    text = clean_text(main.get_text("\n", strip=True))
    text = re.sub(r"(?<=[(\[«¿¡])\n|\n(?=[.,;:!?)\]»%])", "", text)
    return Page(url=url, title=title or unlinked(host_of(url)), description=description, blocks=blocks, text=text, nav=nav)


def load(url: str, force_browser: bool = False) -> Page:
    """Fetch and simplify a page; script-only pages are rendered with Chromium."""
    final_url, markup = url, ""
    if not force_browser:
        response = net.fetch(url, stream=True, timeout=(15, 40))
        try:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code >= 400 and response.status_code not in {401, 403, 429, 503}:
                raise ValueError(f"http_{response.status_code}")
            if content_type and "html" not in content_type and "xml" not in content_type:
                raise NotHtml(content_type)
            data = net.read_limited(response, MAX_PAGE_BYTES)
            final_url = response.url
            markup = decode_page(data, response.headers.get("content-type", ""))
            blocked = response.status_code in {401, 403, 429, 503}
        finally:
            response.close()
    else:
        blocked = True
    page = parse(final_url, markup) if markup else Page(url=final_url)
    weak = len(page.text) < 500 or blocked or re.search(r"enable javascript|habilita javascript|just a moment|checking your browser", page.text[:2000], re.I)
    if weak and browser.available():
        try:
            rendered_url, rendered = browser.render(final_url)
            candidate = parse(rendered_url, rendered)
            if len(candidate.text) > len(page.text):
                page = candidate
        except Exception:
            pass
    if not page.text and not page.blocks:
        raise ValueError("empty_page")
    _cache[state.link_id(page.url)] = page
    for key in [key for key, item in _cache.items() if time.time() - item.created > CACHE_SECONDS]:
        _cache.pop(key, None)
    return page


def cached(url: str) -> Page | None:
    page = _cache.get(state.link_id(url))
    return page if page and time.time() - page.created < CACHE_SECONDS else None


def paginate(page: Page, limit_kb: int) -> list[list[str]]:
    limit = max(20, limit_kb) * 1024
    pages: list[list[str]] = [[]]
    size = 0
    for block in page.blocks:
        encoded = len(block.encode("utf-8"))
        if pages[-1] and size + encoded > limit:
            pages.append([])
            size = 0
        pages[-1].append(block)
        size += encoded
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
    """One reader page for the mini app, small enough for a single webxdc update (each update is an e-mail)."""
    chunks = paginate(page, min(int(state.setting("reader_kb")), APP_PAGE_KB))
    total = len(chunks)
    number = max(1, min(number, total))
    return {"url": page.url, "title": page.title, "host": host_of(page.url), "page": number, "pages": total,
            "html": "\n".join(chunks[number - 1]), "nav": [[label, target] for label, target in page.nav[:20]] if number == 1 else []}


def url_title(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path.rsplit("/", 1)[-1] or parsed.netloc
