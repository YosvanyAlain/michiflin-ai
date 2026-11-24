"""Public HTTP access with redirect validation, automatic cookies and consent defaults."""
from __future__ import annotations

import ipaddress
import socket
import threading
import weakref
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import config, egress
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
    """Treat only globally routable unicast addresses as public."""
    return not egress._public(address)


def validate_public_url(url: str) -> str:
    """Reject non-HTTP schemes and private-network destinations before any request."""
    try:
        normalized = url.strip()
        parsed = urlparse(normalized)
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        raise ValueError("bad_url") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or port is not None and not 1 <= port <= 65535):
        raise ValueError("bad_url")
    host = parsed.hostname.rstrip(".")
    lower = host.lower()
    if not host or lower in {"localhost", "localhost.localdomain"} or lower.endswith(".localhost"):
        raise ValueError("private_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _blocked(host):
            raise ValueError("private_url")
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        raise ValueError("dns") from None
    if not addresses or any(_blocked(address) for address in addresses):
        raise ValueError("private_url")
    return normalized


def _retain_gateway(response: requests.Response, gateway: egress.EgressProxy, stream: bool,
                    owned_session: requests.Session | None = None) -> requests.Response:
    def release() -> None:
        gateway.close()
        if owned_session is not None:
            owned_session.close()

    if not stream:
        release()
        return response
    close_method = weakref.WeakMethod(response.close)
    finalizer = weakref.finalize(response, release)

    def close() -> None:
        try:
            original = close_method()
            if original is not None:
                original()
        finally:
            finalizer()

    response.close = close  # type: ignore[method-assign]
    response._egress_proxy_finalizer = finalizer  # type: ignore[attr-defined]
    return response


def request(method: str, url: str, proxy: bool = False, *, follow_redirects: bool = True,
            anonymous: bool = False, **kwargs: Any) -> requests.Response:
    """Use a DNS-pinning gateway; optionally avoid all stored credentials/cookies."""
    current = validate_public_url(url)
    kwargs["allow_redirects"] = False
    kwargs.setdefault("timeout", (15, 45))
    owned = requests.Session() if anonymous else None
    if owned is not None:
        # A public-book fetch must not inherit a person's browser cookies,
        # .netrc credentials, environment proxies or authorization headers.
        owned.trust_env = False
        owned.headers.update(HEADERS)
    http = owned if owned is not None else session()
    try:
        for _ in range(10):
            upstream = WEB_PROXY if proxy else ""
            gateway_limit = max(1_000_000_000, config.MAX_FILE_BYTES * 3 + 100_000_000)
            gateway = egress.EgressProxy(upstream=upstream, max_bytes=gateway_limit, allow_http=True)
            try:
                request_kwargs = dict(kwargs)
                request_kwargs["proxies"] = {"http": gateway.url, "https": gateway.url}
                response = http.request(method, current, **request_kwargs)
            except BaseException:
                gateway.close()
                raise
            status = response.status_code
            target = response.headers.get("Location") if follow_redirects and status in {301, 302, 303, 307, 308} else None
            if not target:
                retained = _retain_gateway(response, gateway, bool(kwargs.get("stream", False)), owned)
                owned = None  # response.close/finalizer owns the anonymous session
                return retained
            response.close()
            gateway.close()
            current = validate_public_url(urljoin(current, target))
            if status == 303:
                method = "GET"
        raise ValueError("redirects")
    finally:
        if owned is not None:
            owned.close()


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
