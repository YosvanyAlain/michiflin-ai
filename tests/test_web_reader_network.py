"""Loopback-only tests for anonymous thumbnails, redirects and cookie separation."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import threading

from PIL import Image
import pytest

from michiflin import chat, net, reader


@pytest.fixture
def local_server(monkeypatch, tmp_path):
    """Simulate public routes entirely on loopback; real egress is never contacted."""
    observed, gateways = [], []
    output = io.BytesIO()
    Image.new('RGB', (100, 100), (35, 120, 200)).save(output, format='PNG')
    image = output.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, self.headers.get('Cookie', ''), self.headers.get('Authorization', '')))
            if self.path in {'/same', '/cross', '/unsafe'}:
                self.send_response(302)
                if self.path == '/same':
                    target = f'http://127.0.0.1:{self.server.server_port}/same-target'
                elif self.path == '/cross':
                    target = f'http://localhost:{self.server.server_port}/cross-target'
                else:
                    target = f'http://127.0.0.1:{self.server.server_port}/private-target'
                self.send_header('Location', target)
                self.send_header('Set-Cookie', 'ephemeral=synthetic; Path=/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(image)))
            self.end_headers()
            self.wfile.write(image)
        def log_message(self, *_args):
            pass

    class Gateway:
        url = 'http://synthetic-proxy.invalid'
        def __init__(self, **_kwargs):
            self.closed = False
            gateways.append(self)
        def close(self):
            self.closed = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous = chat.sender()
    monkeypatch.setattr(net, '_jars', {})
    monkeypatch.setattr(net, '_local', threading.local())
    monkeypatch.setattr(reader, '_site_pauses', {})
    monkeypatch.setattr(net, 'WEB_PROXY', '')
    monkeypatch.setattr(net.egress, 'EgressProxy', Gateway)
    credentials = tmp_path / 'netrc'
    credentials.write_text('default login synthetic-netrc-user password synthetic-netrc-secret\n')
    credentials.chmod(0o600)
    monkeypatch.setenv('NETRC', str(credentials))
    original_request = net.requests.Session.request
    def request_loopback(self, method, url, **kwargs):
        # The synthetic gateway is not a network proxy: requests go only to the
        # loopback server, while the rest of net.request stays unmodified.
        kwargs['proxies'] = {'http': None, 'https': None}
        return original_request(self, method, url, **kwargs)
    monkeypatch.setattr(net.requests.Session, 'request', request_loopback)
    try:
        yield server, observed, gateways
    finally:
        chat.set_sender(previous)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_anonymous_thumbnails_never_inherit_personal_cookies_or_credentials(local_server, monkeypatch):
    server, observed, gateways = local_server
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)  # loopback fixture only
    chat.set_sender('alice@example.org')
    personal = net.session()
    personal.cookies.set('personal', 'synthetic-personal-cookie', domain='127.0.0.1', path='/')
    personal.cookies.set('personal', 'synthetic-other-host-cookie', domain='localhost', path='/')
    personal.headers['Authorization'] = 'Bearer synthetic-personal-token'
    personal.auth = ('synthetic-user', 'synthetic-password')
    root = f'http://127.0.0.1:{server.server_port}'
    assert reader._tiny_image(root + '/same').startswith('data:image/jpeg;base64,')
    assert reader._tiny_image(root + '/cross').startswith('data:image/jpeg;base64,')
    assert [item[0] for item in observed] == ['/same', '/same-target', '/cross', '/cross-target']
    assert all(not auth and 'personal=' not in cookie for _path, cookie, auth in observed)
    assert observed[0][1] == '' and 'ephemeral=synthetic' in observed[1][1]
    assert observed[2][1] == '' and observed[3][1] == ''  # fresh session and different host
    assert not any(cookie.name == 'ephemeral' for cookie in personal.cookies)
    assert len(gateways) == 4 and all(gateway.closed for gateway in gateways)


def test_real_set_cookie_and_removal_rotate_cache_even_when_values_are_restored(local_server, monkeypatch):
    server, observed, gateways = local_server
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)  # loopback fixture only
    chat.set_sender('alice@example.org')
    jar = net.cookie_jar()
    before = reader._cookie_stamp()
    response = net.request('GET', f'http://127.0.0.1:{server.server_port}/same', stream=True)
    response.close()
    assert any(cookie.name == 'ephemeral' for cookie in jar)
    during = reader._cookie_stamp()
    for cookie in list(jar):
        if cookie.name == 'ephemeral':
            jar.clear(cookie.domain, cookie.path, cookie.name)
    after = reader._cookie_stamp()
    assert before != during and during != after and before != after
    assert len(observed) == 2 and len(gateways) == 2 and all(gateway.closed for gateway in gateways)


def test_eviction_does_not_send_a_thread_local_session_cookie_on_real_loopback(local_server, monkeypatch):
    server, observed, gateways = local_server
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)  # loopback fixture only
    chat.set_sender('alice@example.org')
    old = net.session()
    old.cookies.set('personal', 'synthetic-private-cookie', domain='127.0.0.1', path='/')
    for index in range(260):
        chat.set_sender(f'person{index}@example.org')
        net.cookie_jar()
    chat.set_sender('alice@example.org')
    response = net.request('GET', f'http://127.0.0.1:{server.server_port}/after-eviction', stream=True)
    response.close()
    assert net.session().cookies is not old.cookies
    assert observed and observed[-1][0] == '/after-eviction' and 'personal=' not in observed[-1][1]
    assert all(gateway.closed for gateway in gateways)


def test_redirect_destination_is_revalidated_before_it_is_requested(local_server, monkeypatch):
    server, observed, gateways = local_server
    initial = f'http://127.0.0.1:{server.server_port}/unsafe'
    validate = net.validate_public_url
    def allow_only_synthetic_initial(url):
        return url if url == initial else validate(url)
    monkeypatch.setattr(net, 'validate_public_url', allow_only_synthetic_initial)
    with pytest.raises(ValueError, match='private_url'):
        net.request('GET', initial, stream=True, anonymous=True)
    assert [item[0] for item in observed] == ['/unsafe']
    assert len(gateways) == 1 and gateways[0].closed
