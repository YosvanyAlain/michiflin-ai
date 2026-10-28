"""Offline gateway/proxy checks; no real account, WARP service, or external site.

The fake upstream deliberately maps one vetted public test address to a local
TLS fixture. That mapping exists only in this test process, not production.
"""
from __future__ import annotations

import http.server
import json
import os
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from michiflin import config, egress, sandbox

TEST_HOST = "fixture.example"
TEST_IP = "8.8.8.8"


def _read_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = sock.recv(size - len(result))
        if not block:
            raise EOFError()
        result.extend(block)
    return bytes(result)


def _relay(first: socket.socket, second: socket.socket) -> None:
    sockets = [first, second]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 8)
        if exceptional or not readable:
            return
        for current in readable:
            data = current.recv(65536)
            if not data:
                return
            (second if current is first else first).sendall(data)


class _TLSHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        body = b"<!doctype html><title>Proxy fixture</title><body>Proxy fixture reached</body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@contextmanager
def tls_fixture():
    with tempfile.TemporaryDirectory(prefix="proxy-tls-test-") as directory:
        root = Path(directory)
        key, cert = root / "key.pem", root / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=" + TEST_HOST, "-keyout", str(key), "-out", str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TLSHandler)
        server.paths = []  # type: ignore[attr-defined]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(8)
        try:
            if self.server.mode == "socks":  # type: ignore[attr-defined]
                head = _read_exact(sock, 2)
                if head[0] != 5:
                    return
                methods = _read_exact(sock, head[1])
                if self.server.authenticate:  # type: ignore[attr-defined]
                    if 2 not in methods:
                        return
                    sock.sendall(b"\x05\x02")
                    auth = _read_exact(sock, 2)
                    name = _read_exact(sock, auth[1])
                    password = _read_exact(sock, _read_exact(sock, 1)[0])
                    good = (auth[0] == 1 and name == b"fixture-user" and password == b"fixture-password")
                    sock.sendall(b"\x01\x00" if good else b"\x01\x01")
                    if not good:
                        return
                else:
                    if 0 not in methods:
                        return
                    sock.sendall(b"\x05\x00")
                request = _read_exact(sock, 4)
                if request[:3] != b"\x05\x01\x00":
                    return
                atyp = request[3]
                if atyp == 1:
                    host = socket.inet_ntop(socket.AF_INET, _read_exact(sock, 4))
                elif atyp == 4:
                    host = socket.inet_ntop(socket.AF_INET6, _read_exact(sock, 16))
                elif atyp == 3:
                    host = _read_exact(sock, _read_exact(sock, 1)[0]).decode("ascii")
                else:
                    return
                port = int.from_bytes(_read_exact(sock, 2), "big")
                self.server.destinations.append((atyp, host, port))  # type: ignore[attr-defined]
                remote = socket.create_connection(("127.0.0.1", self.server.fixture_port), timeout=5)  # type: ignore[attr-defined]
                sock.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            else:
                data = bytearray()
                while b"\r\n\r\n" not in data and len(data) < 8192:
                    data.extend(_read_exact(sock, 1))
                lines = bytes(data).split(b"\r\n")
                method, authority, _version = lines[0].decode("ascii").split(" ")
                if method != "CONNECT":
                    return
                self.server.destinations.append(("http", authority, 443))  # type: ignore[attr-defined]
                self.server.auth_seen = any(line.lower().startswith(b"proxy-authorization: basic ") for line in lines[1:])  # type: ignore[attr-defined]
                remote = socket.create_connection(("127.0.0.1", self.server.fixture_port), timeout=5)  # type: ignore[attr-defined]
                sock.sendall(b"HTTP/1.1 200 Connection Established\r\nContent-Length: 0\r\n\r\n")
            try:
                sock.settimeout(None)
                remote.settimeout(None)
                _relay(sock, remote)
            finally:
                remote.close()
        except (EOFError, OSError, ValueError, UnicodeError):
            # The availability probe connects and immediately closes, by design.
            return


class _ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, fixture_port: int, mode: str, authenticate: bool = False):
        self.fixture_port, self.mode, self.authenticate = fixture_port, mode, authenticate
        self.destinations: list[tuple[object, ...]] = []
        self.auth_seen = False
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


@contextmanager
def proxy_fixture(fixture_port: int, mode: str = "socks", authenticate: bool = False):
    server = _ProxyServer(fixture_port, mode, authenticate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextmanager
def test_dns():
    original = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host == TEST_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (TEST_IP, port))]
        return original(host, port, *args, **kwargs)

    with mock.patch.object(egress.socket, "getaddrinfo", side_effect=resolve):
        yield


def _open_through_gateway(path: Path) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10)
    client.connect(str(path))
    client.sendall(b"CONNECT fixture.example:443 HTTP/1.1\r\nHost: fixture.example:443\r\n\r\n")
    headers = bytearray()
    while b"\r\n\r\n" not in headers:
        headers.extend(_read_exact(client, 1))
    if not headers.startswith(b"HTTP/1.1 200"):
        raise AssertionError("gateway rejected a public fixture")
    # The self-signed certificate is accepted only by this disposable test client.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with context.wrap_socket(client, server_hostname=TEST_HOST) as secure:
        secure.sendall(b"GET /fixture HTTP/1.1\r\nHost: fixture.example\r\nConnection: close\r\n\r\n")
        result = bytearray()
        while True:
            data = secure.recv(65536)
            if not data:
                return bytes(result)
            result.extend(data)
            headers, separator, body = bytes(result).partition(b"\r\n\r\n")
            if separator:
                lengths = [line.split(b":", 1)[1].strip() for line in headers.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")]
                if lengths and len(body) >= int(lengths[0]):
                    return bytes(result)


class GatewayProxyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "requires openssl for the local TLS fixture")
    def test_socks5_gateway_uses_vetted_ip_and_keeps_tls_end_to_end(self):
        with tls_fixture() as tls, proxy_fixture(tls.server_port, authenticate=True) as proxy, test_dns(), \
             tempfile.TemporaryDirectory(prefix="gateway-test-") as directory:
            path = Path(directory) / "gate.sock"
            upstream = f"socks5h://fixture-user:fixture-password@127.0.0.1:{proxy.server_address[1]}"
            with egress.Gateway(path, upstream=upstream) as gateway:
                response = _open_through_gateway(path)
                self.assertFalse(gateway.take_proxy_failure())
            self.assertIn(b"Proxy fixture reached", response)
            self.assertIn("/fixture", tls.paths)
            self.assertTrue(proxy.destinations)
            self.assertTrue(all(item == (1, TEST_IP, 443) for item in proxy.destinations))

    @unittest.skipUnless(shutil.which("openssl"), "requires openssl for the local TLS fixture")
    def test_proxy_disappearing_after_start_fails_closed_and_reports_it(self):
        with tls_fixture() as tls, proxy_fixture(tls.server_port) as proxy, test_dns(), \
             tempfile.TemporaryDirectory(prefix="gateway-test-") as directory:
            path = Path(directory) / "gate.sock"
            upstream = f"socks5://127.0.0.1:{proxy.server_address[1]}"
            with egress.Gateway(path, upstream=upstream) as gateway:
                proxy.shutdown()
                proxy.server_close()
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(8)
                try:
                    client.connect(str(path))
                    client.sendall(b"CONNECT fixture.example:443 HTTP/1.1\r\nHost: fixture.example:443\r\n\r\n")
                    response = client.recv(1024)
                finally:
                    client.close()
                self.assertTrue(response.startswith(b"HTTP/1.1 403"))
                self.assertTrue(gateway.take_proxy_failure())
                self.assertFalse(gateway.take_proxy_failure())
            self.assertEqual(tls.paths, [])  # no retry on a direct route

    @unittest.skipUnless(shutil.which("openssl"), "requires openssl for the local TLS fixture")
    def test_http_proxy_connect_receives_numeric_ip_and_authentication(self):
        with tls_fixture() as tls, proxy_fixture(tls.server_port, mode="http") as proxy, test_dns(), \
             tempfile.TemporaryDirectory(prefix="gateway-test-") as directory:
            path = Path(directory) / "gate.sock"
            upstream = f"http://fixture-user:fixture-password@127.0.0.1:{proxy.server_address[1]}"
            with egress.Gateway(path, upstream=upstream):
                response = _open_through_gateway(path)
            self.assertIn(b"Proxy fixture reached", response)
            self.assertEqual(proxy.destinations, [("http", TEST_IP + ":443", 443)])
            self.assertTrue(proxy.auth_seen)


@unittest.skipUnless(os.getenv("MICHIFLIN_SANDBOX_TESTS") == "1", "requires a real Linux browser sandbox")
class BrowserProxyIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "requires openssl for the local TLS fixture")
    def test_real_chromium_crosses_gateway_and_socks_proxy(self):
        with tls_fixture() as tls, proxy_fixture(tls.server_port) as proxy, test_dns():
            upstream = f"socks5://127.0.0.1:{proxy.server_address[1]}"
            original_copy = sandbox.shutil.copyfile

            def copy(src, dst, *args, **kwargs):
                if Path(src).name == "remote_worker.py":
                    # Only the disposable test worker accepts the fixture certificate.
                    text = Path(src).read_text().replace('"device_scale_factor": 2,',
                                                         '"device_scale_factor": 2, "ignore_https_errors": True,')
                    Path(dst).write_text(text)
                    return dst
                return original_copy(src, dst, *args, **kwargs)

            with mock.patch.object(config, "WEB_PROXY", upstream), mock.patch.object(config, "MEDIA_PROXY", ""), \
                 mock.patch.object(sandbox.shutil, "copyfile", side_effect=copy), sandbox.BrowserProcess() as worker:
                self.assertEqual(worker.route, "proxy")
                process = worker.process
                self.assertIsNotNone(process)
                payload = {"url": "https://fixture.example/browser", "storage": None, "detail": "economy"}
                process.stdin.write((json.dumps(payload) + "\n").encode())
                process.stdin.flush()
                self.assertTrue(select.select([process.stdout], [], [], 50)[0], "browser produced no frame")
                frame = json.loads(process.stdout.readline(3_000_001))
                self.assertEqual(frame.get("kind"), "frame")
                self.assertFalse(worker.gateway.take_proxy_failure())
            self.assertIn("/browser", tls.paths)
            self.assertTrue(proxy.destinations)
            self.assertTrue(all(item == (1, TEST_IP, 443) for item in proxy.destinations))


if __name__ == "__main__":
    unittest.main()
