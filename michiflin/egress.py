"""An HTTPS CONNECT gateway for network-isolated workers.

Resolve every destination once, reject *any* non-public address, and connect
only to the vetted socket address. Workers have no external network route;
this gateway is the only permitted path out. No URL, header, payload or cookie
is logged. TLS remains end-to-end between the worker and the destination.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_HEADER = 8192
MAX_CLIENTS = 20
IDLE_SECONDS = 40
MAX_TUNNEL_SECONDS = 180


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


def _connect(host: str, port: int) -> socket.socket:
    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise EgressError("dns") from None
    # Mixed public/private answers are rejected, not merely filtered.
    if not candidates or any(not _public(item[4][0]) for item in candidates):
        raise EgressError("private")
    for family, kind, protocol, _name, address in candidates[:8]:
        remote = socket.socket(family, kind, protocol)
        try:
            remote.settimeout(8)
            remote.connect(address)  # no second hostname lookup (DNS rebinding defense)
            remote.settimeout(None)
            return remote
        except OSError:
            remote.close()
    raise EgressError("connect")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path: str, max_bytes: int):
        self.limit = threading.BoundedSemaphore(MAX_CLIENTS)
        self.remaining = max_bytes
        self.budget_lock = threading.Lock()
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
            remote = _connect(host, port)
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
        except (OSError, ValueError, UnicodeError, EgressError):
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

    def __init__(self, socket_path: Path, max_bytes: int = 100_000_000):
        self.path = socket_path
        self.server = _Server(str(socket_path), max_bytes)
        socket_path.chmod(0o666)  # reachable by the unprivileged socat inside the worker namespace
        self.thread = threading.Thread(target=self.server.serve_forever, name="egress", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Gateway:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
