"""Local SSRF/DNS-rebinding regressions; all network endpoints are loopback fixtures."""
from __future__ import annotations

import http.server
import select
import socket
import socketserver
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit
from unittest import mock

import pytest

from michiflin import egress, net

TEST_HOST = "fixture.example"
TEST_IP = "8.8.8.8"


class _OriginHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.requests_seen.append((self.path, self.headers.get("Host")))  # type: ignore[attr-defined]
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"pinned fixture reached"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@contextmanager
def origin_fixture():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OriginHandler)
    server.requests_seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _dns_answer(address: str, port: int | None) -> list[tuple[int, int, int, str, tuple[str, int | None]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]


class _ForwardProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        raw = bytearray()
        try:
            self.request.settimeout(5)
            while b"\r\n\r\n" not in raw and len(raw) <= 8192:
                block = self.request.recv(1024)
                if not block:
                    return  # The proxy availability probe connects and closes.
                raw.extend(block)
            header, separator, pending = bytes(raw).partition(b"\r\n\r\n")
            if not separator:
                return
            lines = header.split(b"\r\n")
            method, target, version = lines[0].decode("ascii").split(" ")
            parsed = urlsplit(target)
            self.server.targets.append(target)  # type: ignore[attr-defined]
            if parsed.hostname != TEST_IP or parsed.port != self.server.origin_port:  # type: ignore[attr-defined]
                self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            forwarded = [f"{method} {path} {version}".encode("ascii")]
            forwarded.extend(line for line in lines[1:] if not line.lower().startswith(b"proxy-authorization:"))
            remote = socket.create_connection(("127.0.0.1", self.server.origin_port), timeout=5)  # type: ignore[attr-defined]
            remote.settimeout(None)
            self.request.settimeout(None)
            remote.sendall(b"\r\n".join(forwarded) + b"\r\n\r\n" + pending)
            sockets = [self.request, remote]
            while True:
                readable, _, exceptional = select.select(sockets, [], sockets, 8)
                if exceptional or not readable:
                    return
                for source in readable:
                    block = source.recv(65536)
                    if not block:
                        return
                    (remote if source is self.request else self.request).sendall(block)
        except (OSError, ValueError, UnicodeError):
            return


class _ForwardProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, origin_port: int):
        self.origin_port = origin_port
        self.targets: list[str] = []
        super().__init__(("127.0.0.1", 0), _ForwardProxyHandler)


@contextmanager
def forward_proxy(origin_port: int):
    server = _ForwardProxy(origin_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cgnat_and_ipv4_mapped_addresses_are_not_public():
    for address in ("100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1", "::ffff:127.0.0.1"):
        assert net._blocked(address)
        with pytest.raises(ValueError, match="private_url"):
            net.validate_public_url(f"http://[{address}]/" if ":" in address else f"http://{address}/")


def test_gateway_rejects_mixed_cgnat_and_mapped_dns_answers_before_connecting():
    public = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
    for address in ("127.0.0.1", "100.64.0.1", "::ffff:100.64.0.1"):
        if ":" in address:
            private = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443, 0, 0))
        else:
            private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        with mock.patch.object(egress.socket, "getaddrinfo", return_value=[public, private]):
            with mock.patch.object(egress.socket, "socket") as create_socket:
                with pytest.raises(egress.EgressError, match="private"):
                    egress._connect(TEST_HOST, 443)
                create_socket.assert_not_called()


def test_rebinding_to_loopback_after_prevalidation_fails_closed():
    with origin_fixture() as origin:
        answers = iter((TEST_IP, "127.0.0.1"))
        original = socket.getaddrinfo
        calls = []

        def resolve(host, port, *args, **kwargs):
            if host == TEST_HOST:
                address = next(answers)
                calls.append(address)
                return _dns_answer(address, port)
            return original(host, port, *args, **kwargs)

        with mock.patch.object(net.socket, "getaddrinfo", side_effect=resolve):
            response = net.request("GET", f"http://{TEST_HOST}:{origin.server_port}/rebinding")
            try:
                assert response.status_code == 403
                assert response.content == b""
            finally:
                response.close()

        assert calls == [TEST_IP, "127.0.0.1"]
        assert origin.requests_seen == []


def test_request_connects_to_the_vetted_numeric_address_not_a_second_dns_answer():
    with origin_fixture() as origin:
        original_resolve = socket.getaddrinfo
        original_connect = socket.socket.connect
        calls = []

        def resolve(host, port, *args, **kwargs):
            if host == TEST_HOST:
                calls.append(host)
                return _dns_answer(TEST_IP, port)
            return original_resolve(host, port, *args, **kwargs)

        def connect(sock, address):
            if isinstance(address, tuple) and address[0] == TEST_IP:
                address = ("127.0.0.1", origin.server_port)
            return original_connect(sock, address)

        with mock.patch.object(net.socket, "getaddrinfo", side_effect=resolve), \
             mock.patch.object(socket.socket, "connect", new=connect):
            response = net.request("GET", f"http://{TEST_HOST}:{origin.server_port}/pinned?q=1", stream=True)
            try:
                assert response.status_code == 200
                assert b"".join(response.iter_content(chunk_size=8)) == b"pinned fixture reached"
            finally:
                response.close()

        # One preflight resolution and one gateway resolution; the connection
        # uses the numeric address selected by the latter, never the hostname.
        assert calls == [TEST_HOST, TEST_HOST]
        assert origin.requests_seen == [("/pinned?q=1", f"{TEST_HOST}:{origin.server_port}")]


def test_request_can_return_a_redirect_without_contacting_its_destination():
    with origin_fixture() as origin:
        original_resolve = socket.getaddrinfo
        original_connect = socket.socket.connect
        calls = []

        def resolve(host, port, *args, **kwargs):
            if host == TEST_HOST:
                calls.append(host)
                return _dns_answer(TEST_IP, port)
            return original_resolve(host, port, *args, **kwargs)

        def connect(sock, address):
            if isinstance(address, tuple) and address[0] == TEST_IP:
                address = ("127.0.0.1", origin.server_port)
            return original_connect(sock, address)

        with mock.patch.object(net.socket, "getaddrinfo", side_effect=resolve), \
             mock.patch.object(socket.socket, "connect", new=connect):
            response = net.request("GET", f"http://{TEST_HOST}:{origin.server_port}/redirect",
                                   follow_redirects=False)
            try:
                assert response.status_code == 302
                assert response.headers["Location"] == "http://127.0.0.1/private"
            finally:
                response.close()

        assert calls == [TEST_HOST, TEST_HOST]
        assert origin.requests_seen == [("/redirect", f"{TEST_HOST}:{origin.server_port}")]


def test_configured_http_proxy_receives_only_a_vetted_numeric_origin():
    with origin_fixture() as origin, forward_proxy(origin.server_port) as upstream:
        original = socket.getaddrinfo
        lookups = []

        def resolve(host, port, *args, **kwargs):
            if host == TEST_HOST:
                lookups.append(host)
                return _dns_answer(TEST_IP, port)
            return original(host, port, *args, **kwargs)

        proxy_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        with mock.patch.object(net, "WEB_PROXY", proxy_url), \
             mock.patch.object(net.socket, "getaddrinfo", side_effect=resolve):
            response = net.request("GET", f"http://{TEST_HOST}:{origin.server_port}/via-proxy", proxy=True)
            try:
                assert response.status_code == 200
                assert response.text == "pinned fixture reached"
            finally:
                response.close()

        assert len(lookups) == 2
        assert upstream.targets == [f"http://{TEST_IP}:{origin.server_port}/via-proxy"]
        assert origin.requests_seen == [("/via-proxy", f"{TEST_HOST}:{origin.server_port}")]
