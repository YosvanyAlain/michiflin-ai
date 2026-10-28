"""An HTTPS CONNECT gateway for network-isolated workers.

Resolve every destination once, reject *any* non-public address, and connect
only to the vetted socket address, directly or through a configured upstream
proxy. Workers have no external network route; this gateway is their only path
out. No URL, header, payload, cookie or proxy credential is logged. TLS remains
end-to-end between the worker and the destination.
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    import socks
except ImportError:  # direct egress must still work without the optional module
    socks = None  # type: ignore[assignment]

MAX_HEADER = 8192
MAX_CLIENTS = 20
IDLE_SECONDS = 40
MAX_TUNNEL_SECONDS = 180
MAX_CONNECT_SECONDS = 15
PROXY_PROBE_SECONDS = 3


class EgressError(Exception):
    pass


def _public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if isinstance(address, ipaddress.IPv6Address):
        # Exclude translated/tunneled IPv6 (NAT64, 6to4 and Teredo), which can
        # otherwise encode an internal IPv4 destination inside a global address.
        if (address not in ipaddress.ip_network("2000::/3") or address.sixtofour is not None
                or address.teredo is not None):
            return False
    return bool(address.is_global and not (address.is_reserved or address.is_multicast or address.is_unspecified))


def _authority(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit("https://" + value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise EgressError("authority") from None
    if (not host or port != 443 or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or len(host) > 253):
        raise EgressError("authority")
    return host, port


@dataclass(frozen=True, repr=False)
class _Proxy:
    # Credentials belong to the host-side gateway only, never the worker.
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def _proxy(value: str) -> _Proxy:
    """Parse an administrator-configured upstream without exposing its value."""
    try:
        if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        scheme, host = parsed.scheme.lower(), parsed.hostname
        if (scheme not in {"socks5", "socks5h", "http"} or not host
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError()
        port = parsed.port if parsed.port is not None else (80 if scheme == "http" else 1080)
        if not 1 <= port <= 65535 or (scheme.startswith("socks5") and socks is None):
            raise ValueError()
        if (parsed.username is None) != (parsed.password is None):
            raise ValueError()
        username = unquote(parsed.username or "", errors="strict")
        password = unquote(parsed.password or "", errors="strict")
        if parsed.username is not None and (not username or not password):
            raise ValueError()
        # PySocks encodes the SOCKS length in one ASCII byte, so keep its
        # credentials in the range it can transmit correctly.
        limit = 127 if scheme.startswith("socks5") else 255
        for item in (username, password):
            if len(item.encode("utf-8")) > limit or any(ord(c) < 32 or ord(c) == 127 for c in item):
                raise ValueError()
        return _Proxy(scheme, host, port, username, password)
    except (ValueError, UnicodeError):
        raise EgressError("proxy_invalid") from None


def _probe_proxy(proxy: _Proxy) -> None:
    """Fail quickly if a configured upstream is not even listening.

    This does not claim that WARP is connected or that a particular site will
    accept it. The workflow independently verifies WARP before configuring it.
    """
    try:
        with socket.create_connection((proxy.host, proxy.port), timeout=PROXY_PROBE_SECONDS):
            pass
    except OSError:
        raise EgressError("proxy_unavailable") from None


def _via_socks(proxy: _Proxy, address: str, port: int, timeout: float) -> socket.socket:
    """Send the *already validated numeric address* in the SOCKS CONNECT."""
    if socks is None:
        raise EgressError("proxy_invalid")
    deadline = time.monotonic() + timeout
    try:
        proxies = socket.getaddrinfo(proxy.host, proxy.port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise EgressError("proxy_unavailable") from None
    for family, kind, protocol, _name, proxy_address in proxies[:8]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        remote = socks.socksocket(family, kind, protocol)
        try:
            # Even for socks5h, the destination is a numeric address, never a
            # hostname that the proxy could resolve again to a private IP.
            remote.set_proxy(socks.SOCKS5, proxy_address[0], proxy_address[1], rdns=False,
                             username=proxy.username or None, password=proxy.password or None)
            remote.settimeout(remaining)
            remote.connect((address, port), catch_errors=True)
            return remote
        except socks.SOCKS5Error:
            remote.close()
            raise EgressError("connect") from None  # upstream reached; destination refused
        except (OSError, ValueError):
            remote.close()
    raise EgressError("proxy_unavailable")


def _via_http(proxy: _Proxy, address: str, port: int, timeout: float) -> socket.socket:
    """Tunnel an already validated numeric destination via an HTTP proxy."""
    connection = http.client.HTTPConnection(proxy.host, proxy.port, timeout=timeout)
    authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    headers = {"Host": authority}
    if proxy.username:
        token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8")).decode("ascii")
        headers["Proxy-Authorization"] = "Basic " + token
    try:
        connection.set_tunnel(address, port, headers=headers)
        connection.connect()
        remote = connection.sock
        if remote is None:
            raise OSError()
        connection.sock = None  # retain the tunnel after the helper goes out of scope
        return remote
    except (OSError, ValueError, http.client.HTTPException):
        connection.close()
        raise EgressError("proxy_unavailable") from None


def _connect(host: str, port: int, upstream: _Proxy | None = None) -> socket.socket:
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise EgressError("dns") from None
    # Mixed public/private answers are rejected, not merely filtered. The
    # upstream is given only these numeric addresses, not the hostname.
    if not candidates or any("%" in item[4][0] or not _public(item[4][0]) for item in candidates):
        raise EgressError("private")
    deadline = time.monotonic() + MAX_CONNECT_SECONDS
    for family, kind, protocol, _name, address in candidates[:8]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if upstream is not None:
            try:
                remote = (_via_http(upstream, address[0], port, remaining) if upstream.scheme == "http"
                          else _via_socks(upstream, address[0], port, remaining))
                remote.settimeout(None)
                return remote
            except EgressError as exc:
                if str(exc) != "connect":
                    raise
                continue
        remote = socket.socket(family, kind, protocol)
        try:
            remote.settimeout(min(8, remaining))
            remote.connect(address)  # no second hostname lookup (DNS rebinding defense)
            remote.settimeout(None)
            return remote
        except OSError:
            remote.close()
    # A reachable SOCKS proxy can still refuse every vetted destination (for
    # example if WARP disconnected). Report that without retrying directly.
    raise EgressError("proxy_unavailable" if upstream is not None else "connect")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, max_bytes: int, upstream: _Proxy | None):
        self.limit = threading.BoundedSemaphore(MAX_CLIENTS)
        self.remaining = max_bytes
        self.budget_lock = threading.Lock()
        self.upstream = upstream
        self.proxy_failure = threading.Event()
        super().__init__(path, _Handler)

    def spend(self, count: int) -> bool:
        with self.budget_lock:
            self.remaining -= count
            return self.remaining >= 0

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.limit.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.limit.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.limit.release()


class _Handler(socketserver.BaseRequestHandler):
    server: _Server
    request: socket.socket

    def handle(self) -> None:
        remote: socket.socket | None = None
        established = False
        try:
            self.request.settimeout(8)
            data = b""
            while b"\r\n\r\n" not in data and len(data) <= MAX_HEADER:
                piece = self.request.recv(min(1024, MAX_HEADER + 1 - len(data)))
                if not piece:
                    return
                data += piece
            headers, separator, pending = data.partition(b"\r\n\r\n")
            if not separator or len(headers) > MAX_HEADER:
                raise EgressError("headers")
            first = headers.split(b"\r\n", 1)[0].decode("ascii", "strict").split(" ")
            if len(first) != 3 or first[0] != "CONNECT" or first[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                raise EgressError("method")
            host, port = _authority(first[1])
            remote = _connect(host, port, self.server.upstream)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            established = True
            self.request.settimeout(None)
            if pending:
                if not self.server.spend(len(pending)):
                    return
                remote.sendall(pending)
            sockets = [self.request, remote]
            until = time.monotonic() + MAX_TUNNEL_SECONDS
            while time.monotonic() < until:
                readable, _, exceptional = select.select(sockets, [], sockets, IDLE_SECONDS)
                if exceptional or not readable:
                    return
                for source in readable:
                    block = source.recv(65536)
                    if not block or not self.server.spend(len(block)):
                        return
                    (remote if source is self.request else self.request).sendall(block)
        except (OSError, ValueError, UnicodeError, EgressError) as exc:
            if isinstance(exc, EgressError) and str(exc) == "proxy_unavailable":
                self.server.proxy_failure.set()
            if not established:
                try:
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
        finally:
            if remote is not None:
                remote.close()


class Gateway:
    """A private socket; place it in a non-listable randomized directory."""

    def __init__(self, socket_path: Path, max_bytes: int = 100_000_000, upstream: str = ""):
        self.path = socket_path
        proxy = _proxy(upstream) if upstream else None
        if proxy is not None:
            _probe_proxy(proxy)
        self.server = _Server(str(socket_path), max_bytes, proxy)
        try:
            socket_path.chmod(0o666)  # reachable by the unprivileged socat inside the worker namespace
            self.thread = threading.Thread(target=self.server.serve_forever, name="egress", daemon=True)
            self.thread.start()
        except BaseException:
            self.server.server_close()
            socket_path.unlink(missing_ok=True)
            raise

    def take_proxy_failure(self) -> bool:
        failed = self.server.proxy_failure.is_set()
        self.server.proxy_failure.clear()
        return failed

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Gateway:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
