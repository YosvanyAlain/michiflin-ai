"""Public HTTP access with redirect validation, automatic cookies and consent defaults."""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .config import WEB_PROXY

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}
# Pre-accepted consent cookies for common consent walls (Google/YouTube and generic CMPs).
CONSENT_COOKIES = [
    ("CONSENT", "YES+cb.20240101-00-p0.es+FX+000", ".google.com"),
    ("CONSENT", "YES+cb.20240101-00-p0.es+FX+000", ".youtube.com"),
    ("SOCS", "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMTAxLjA3X3AwGgJlcyACGgYIgLnQrgY", ".google.com"),
    ("SOCS", "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMTAxLjA3X3AwGgJlcyACGgYIgLnQrgY", ".youtube.com"),
]

_local = threading.local()
_jars: dict[str, requests.cookies.RequestsCookieJar] = {}
_jars_lock = threading.Lock()


def _identity() -> str:
    # Import lazily to avoid making the network layer part of chat's import graph.
    try:
        from . import chat
        address = chat.sender().strip().lower()
    except Exception:
        address = ""
    return address or f"thread:{threading.get_ident()}"


def _jar(identity: str) -> requests.cookies.RequestsCookieJar:
    with _jars_lock:
        jar = _jars.get(identity)
        if jar is None:
            jar = requests.cookies.RequestsCookieJar()
            for name, value, domain in CONSENT_COOKIES:
                jar.set(name, value, domain=domain, path="/")
            _jars[identity] = jar
            # Avoid unbounded cookie retention if the bot is eventually shared widely.
            while len(_jars) > 256:
                _jars.pop(next(iter(_jars)))
        return jar


def session() -> requests.Session:
    """Thread-local HTTP session bound to the current contact's private cookie jar."""
    identity = _identity()
    current = getattr(_local, "session", None)
    if current is None or getattr(_local, "identity", None) != identity:
        current = requests.Session()
        current.headers.update(HEADERS)
        current.cookies = _jar(identity)
        _local.session = current
        _local.identity = identity
    return current


def cookie_jar() -> requests.cookies.RequestsCookieJar:
    """Cookie jar for the current contact (used to seed a private browser context)."""
    return _jar(_identity())


def _blocked(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def validate_public_url(url: str) -> str:
    """Reject non-HTTP schemes and private-network destinations before any request."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("bad_url")
    host = parsed.hostname.rstrip(".")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("private_url")
    try:
        if _blocked(host):
            raise ValueError("private_url")
    except ValueError as exc:
        if str(exc) == "private_url":
            raise
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError("dns") from None
    if any(_blocked(address) for address in addresses):
        raise ValueError("private_url")
    return url.strip()


def request(method: str, url: str, proxy: bool = False, **kwargs: Any) -> requests.Response:
    """Follow redirects manually so each hop is validated; cookies are kept automatically."""
    current = validate_public_url(url)
    kwargs["allow_redirects"] = False
    kwargs.setdefault("timeout", (15, 45))
    if proxy and WEB_PROXY:
        kwargs["proxies"] = {"http": WEB_PROXY, "https": WEB_PROXY}
    http = session()
    for _ in range(10):
        response = http.request(method, current, **kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        target = response.headers.get("Location")
        response.close()
        if not target:
            return response
        current = validate_public_url(urljoin(current, target))
        if response.status_code == 303:
            method = "GET"
    raise ValueError("redirects")


def fetch(url: str, **kwargs: Any) -> requests.Response:
    """GET with a single retry through the optional egress proxy when a site blocks us."""
    try:
        response = request("GET", url, **kwargs)
        if response.status_code in {403, 429, 451, 503} and WEB_PROXY:
            response.close()
            return request("GET", url, proxy=True, **kwargs)
        return response
    except requests.RequestException:
        if not WEB_PROXY:
            raise
        return request("GET", url, proxy=True, **kwargs)


def read_limited(response: requests.Response, maximum: int) -> bytes:
    data = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if chunk:
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("too_large")
    return bytes(data)
