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
import shutil
import socket
import socketserver
import tempfile
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
# socketserver's shutdown() only returns once serve_forever notices the flag,
# so the default 0.5 s poll interval is paid as latency on every request that
# builds a gateway. These proxies are ephemeral; poll often and close fast.
POLL_SECONDS = 0.02
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


def _authority(value: str, *, allow_nonstandard_port: bool = False) -> tuple[str, int]:
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise EgressError("authority")
    try:
        parsed = urlsplit("https://" + value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise EgressError("authority") from None
    if (not host or port is None or not 1 <= port <= 65535
            or (port != 443 and not allow_nonstandard_port) or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or len(host) > 253 or "%" in host):
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
        connect_host = f"[{address}]" if ":" in address else address
        connection.set_tunnel(connect_host, port, headers=headers)
        connection.connect()
        remote = connection.sock
        if remote is None:
            raise OSError()
        connection.sock = None  # retain the tunnel after the helper goes out of scope
        return remote
    except (OSError, ValueError, http.client.HTTPException):
        connection.close()
        raise EgressError("proxy_unavailable") from None


def _resolve_public(host: str, port: int) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise EgressError("dns") from None
    # Mixed public/private answers are rejected, not merely filtered. Every
    # returned connection target is numeric and already vetted.
    if not candidates or any("%" in item[4][0] or not _public(item[4][0]) for item in candidates):
        raise EgressError("private")
    return candidates


def _connect(host: str, port: int, upstream: _Proxy | None = None) -> socket.socket:
    candidates = _resolve_public(host, port)
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

    def __init__(self, path: str, max_bytes: int, upstream: _Proxy | None, allow_http: bool = False):
        self.limit = threading.BoundedSemaphore(MAX_CLIENTS)
        self.remaining = max_bytes
        self.budget_lock = threading.Lock()
        self.upstream = upstream
        self.allow_http = allow_http
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


def _parse_http_request(first: bytes, fields: list[bytes]) -> tuple[str, int, bytes, bytes, bytes, bytes, list[bytes]]:
    """Parse an absolute-form HTTP request and strip proxy hop-by-hop headers."""
    parts = first.split(b" ")
    if (len(parts) != 3 or parts[0] not in {b"GET", b"HEAD", b"POST", b"PUT", b"PATCH", b"DELETE", b"OPTIONS", b"TRACE"}
            or parts[2] not in {b"HTTP/1.0", b"HTTP/1.1"}):
        raise EgressError("method")
    try:
        target = parts[1].decode("ascii", "strict")
        if any(ord(char) <= 32 or ord(char) == 127 for char in target) or "#" in target:
            raise ValueError()
        parsed = urlsplit(target)
        host, explicit_port = parsed.hostname, parsed.port
        port = explicit_port if explicit_port is not None else 80
    except (UnicodeError, ValueError):
        raise EgressError("authority") from None
    if (parsed.scheme.lower() != "http" or not host or len(host) > 253 or "%" in host
            or parsed.username is not None or parsed.password is not None or parsed.fragment
            or not 1 <= port <= 65535 or not parsed.netloc):
        raise EgressError("authority")
    try:
        host_ascii = ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            host_ascii = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise EgressError("authority") from None
    host_header = f"[{host_ascii}]" if ":" in host_ascii else host_ascii
    if explicit_port is not None:
        host_header += f":{port}"
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    connection_tokens: set[bytes] = set()
    parsed_fields: list[tuple[bytes, bytes]] = []
    token_chars = b"!#$%&'*+-.^_`|~"
    for field in fields:
        name, separator, value = field.partition(b":")
        if (not separator or not name or field[:1] in {b" ", b"\t"}
                or any(not (48 <= char <= 57 or 65 <= char <= 90 or 97 <= char <= 122 or char in token_chars) for char in name)):
            raise EgressError("headers")
        if any((char < 32 and char != 9) or char == 127 for char in value):
            raise EgressError("headers")
        lowered = name.lower()
        if lowered == b"connection":
            connection_tokens.update(item.strip().lower() for item in value.split(b",") if item.strip())
        parsed_fields.append((lowered, field))
    blocked = connection_tokens | {b"host", b"connection", b"keep-alive", b"proxy-authorization", b"proxy-connection", b"upgrade"}
    safe_fields = [field for name, field in parsed_fields if name not in blocked]
    return host, port, parts[0], parts[2], path.encode("ascii"), host_header.encode("ascii"), safe_fields


def _serialize_http_request(method: bytes, version: bytes, path: bytes, host_header: bytes,
                            fields: list[bytes], *, target_ip: str = "", target_port: int = 80,
                            proxy: _Proxy | None = None) -> bytes:
    if proxy is not None:
        address = f"[{target_ip}]" if ":" in target_ip else target_ip
        target = f"http://{address}:{target_port}".encode("ascii") + path
    else:
        target = path
    lines = [method + b" " + target + b" " + version, b"Host: " + host_header, *fields]
    if proxy is not None and proxy.username:
        credentials = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("utf-8"))
        lines.append(b"Proxy-Authorization: Basic " + credentials)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


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
            raw_headers, separator, pending = data.partition(b"\r\n\r\n")
            if not separator or len(raw_headers) > MAX_HEADER:
                raise EgressError("headers")
            lines = raw_headers.split(b"\r\n")
            first_line, fields = lines[0], lines[1:]
            parts = first_line.split(b" ")
            if len(parts) != 3:
                raise EgressError("method")
            outbound = pending
            if parts[0] == b"CONNECT":
                try:
                    authority = parts[1].decode("ascii", "strict")
                except UnicodeError:
                    raise EgressError("authority") from None
                if parts[2] not in {b"HTTP/1.0", b"HTTP/1.1"}:
                    raise EgressError("method")
                host, port = _authority(authority, allow_nonstandard_port=self.server.allow_http)
                remote = _connect(host, port, self.server.upstream)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                established = True
            else:
                if not self.server.allow_http:
                    raise EgressError("method")
                host, port, method, version, path, host_header, safe_fields = _parse_http_request(first_line, fields)
                upstream = self.server.upstream
                if upstream is not None and upstream.scheme == "http":
                    # An upstream HTTP proxy receives a numeric absolute URI,
                    # never the original hostname that it could resolve again.
                    address = _resolve_public(host, port)[0][4][0]
                    try:
                        remote = socket.create_connection((upstream.host, upstream.port), timeout=MAX_CONNECT_SECONDS)
                    except OSError:
                        raise EgressError("proxy_unavailable") from None
                    outbound = _serialize_http_request(method, version, path, host_header, safe_fields,
                                                       target_ip=address, target_port=port, proxy=upstream) + pending
                else:
                    remote = _connect(host, port, upstream)
                    outbound = _serialize_http_request(method, version, path, host_header, safe_fields) + pending
                if not self.server.spend(len(outbound)):
                    return
                remote.sendall(outbound)
                established = True
            self.request.settimeout(None)
            remote.settimeout(None)
            if parts[0] == b"CONNECT" and outbound:
                if not self.server.spend(len(outbound)):
                    return
                remote.sendall(outbound)
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
    """A private Unix socket; place it in a non-listable randomized directory."""

    def __init__(self, socket_path: Path, max_bytes: int = 100_000_000, upstream: str = "", allow_http: bool = False):
        self.path = socket_path
        proxy = _proxy(upstream) if upstream else None
        if proxy is not None:
            _probe_proxy(proxy)
        self.server = _Server(str(socket_path), max_bytes, proxy, allow_http)
        try:
            socket_path.chmod(0o666)  # reachable by the unprivileged socat inside the worker namespace
            self.thread = threading.Thread(target=self.server.serve_forever, args=(POLL_SECONDS,),
                                           name="egress", daemon=True)
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


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, socket_path: Path):
        self.socket_path = str(socket_path)
        self.active: set[socket.socket] = set()
        self.active_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _RelayHandler)

    def close_active_connections(self) -> None:
        with self.active_lock:
            active = list(self.active)
        for current in active:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            current.close()


class _RelayHandler(socketserver.BaseRequestHandler):
    server: _RelayServer

    def handle(self) -> None:
        remote = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with self.server.active_lock:
            self.server.active.add(self.request)
        try:
            remote.settimeout(5)
            remote.connect(self.server.socket_path)
            remote.settimeout(None)
            self.request.settimeout(None)
            sockets = [self.request, remote]
            until = time.monotonic() + MAX_TUNNEL_SECONDS
            while time.monotonic() < until:
                readable, _, exceptional = select.select(sockets, [], sockets, IDLE_SECONDS)
                if exceptional or not readable:
                    return
                for source in readable:
                    block = source.recv(65536)
                    if not block:
                        return
                    (remote if source is self.request else self.request).sendall(block)
        except OSError:
            return
        finally:
            with self.server.active_lock:
                self.server.active.discard(self.request)
            remote.close()


class EgressProxy:
    """Short-lived loopback HTTP proxy backed by the same DNS-pinning gateway.

    Browser and library clients can use an ordinary HTTP proxy URL while all
    origin name resolution, public-address checks and numeric connections stay
    in Gateway. Only the listener's random port is exposed on loopback.
    """

    def __init__(self, upstream: str = "", max_bytes: int = 1_000_000_000, allow_http: bool = True):
        self.root = Path(tempfile.mkdtemp(prefix="michiflin-egress-"))
        self.root.chmod(0o700)
        self.gateway: Gateway | None = None
        self.server: _RelayServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""
        self._closed = False
        try:
            path = self.root / "gate.sock"
            self.gateway = Gateway(path, max_bytes=max_bytes, upstream=upstream, allow_http=allow_http)
            self.server = _RelayServer(path)
            self.thread = threading.Thread(target=self.server.serve_forever, args=(POLL_SECONDS,),
                                           name="egress-relay", daemon=True)
            self.thread.start()
            self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.server is not None:
                if self.thread is not None and self.thread.is_alive():
                    self.server.shutdown()
                self.server.close_active_connections()
                self.server.server_close()
                if self.thread is not None:
                    self.thread.join(timeout=3)
        finally:
            try:
                if self.gateway is not None:
                    self.gateway.close()
            finally:
                shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> EgressProxy:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
