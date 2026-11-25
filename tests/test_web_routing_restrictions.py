"""Synthetic URL routing restrictions; no real site, account or network is used."""
from __future__ import annotations

import logging

import pytest
from requests.structures import CaseInsensitiveDict

from michiflin import browser, files, media, net, progress, reader, web


class Response:
    def __init__(self, url, status=200, body=b'', kind='text/html; charset=utf-8'):
        self.url, self.status_code, self.body = url, status, body
        self.headers = CaseInsensitiveDict({'Content-Type': kind, 'Content-Length': str(len(body))})
        self.ok = status < 400
        self.closed = False
        self.consumed = 0

    @property
    def text(self):
        return self.body.decode('utf-8', 'replace')

    def iter_content(self, chunk_size=65536):
        for position in range(0, len(self.body), chunk_size):
            part = self.body[position:position + chunk_size]
            self.consumed += len(part)
            yield part

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(reader, '_site_pauses', {})
    monkeypatch.setattr(net, 'WEB_PROXY', 'http://synthetic-proxy.invalid')
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)
    monkeypatch.setattr(progress, 'phase', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(media, 'available', lambda: False)


@pytest.mark.parametrize('status', [401, 403, 429, 451, 503])
def test_blocked_probe_does_not_retry_proxy_or_reader(monkeypatch, status):
    url = 'https://fixture.example/article?token=synthetic-secret'
    calls, replies, responses = [], [], []
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy, kwargs.get('stream')))
        result = Response(target, status=status)
        responses.append(result)
        return result
    monkeypatch.setattr(net, 'request', request)
    monkeypatch.setattr(web.chat, 'text', lambda _chat_id, message: replies.append(message))
    monkeypatch.setattr(web, 'read', lambda *_args: pytest.fail('blocked route must not invoke reader'))
    monkeypatch.setattr(web, 'file_card', lambda *_args: pytest.fail('blocked route must not show a file card'))
    web.route(17, url, 'es')
    assert calls == [('GET', url, False, True)]
    assert responses[0].closed and replies and 'synthetic-secret' not in replies[0]
    if status == 429:
        assert reader._site_paused(url)
        web.route(17, url, 'es')
        assert len(calls) == 1 and 'Espera' in replies[-1]


def test_http_200_verification_page_stops_routing_and_pauses_redirect_host(monkeypatch):
    original, final = 'https://fixture.example/start', 'https://challenge.example/blocked'
    calls, replies, responses = [], [], []
    body = b'<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>'
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy))
        response = Response(final, body=body)
        responses.append(response)
        return response
    monkeypatch.setattr(net, 'request', request)
    monkeypatch.setattr(web.chat, 'text', lambda _chat_id, message: replies.append(message))
    monkeypatch.setattr(web, 'read', lambda *_args: pytest.fail('verification page must not be retried'))
    monkeypatch.setattr(web, 'file_card', lambda *_args: pytest.fail('verification page is not a file'))
    web.route(17, original, 'es')
    assert calls == [('GET', original, False)]
    assert responses[0].closed and 'verificación' in replies[-1]
    assert reader._site_paused(original) and reader._site_paused(final)
    web.route(17, original, 'es')
    assert len(calls) == 1 and 'Espera' in replies[-1]


def test_mediafire_challenge_is_never_resolved_or_probed_again(monkeypatch):
    url = 'https://www.mediafire.com/file/fixture/example'
    body = (b'<title>Just a moment...</title>Checking your browser '
            b'<a href="https://download1.mediafire.com/fixture/file.pdf">Download</a>')
    calls, replies, responses = [], [], []
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy, kwargs.get('stream')))
        response = Response(target, body=body)
        responses.append(response)
        return response
    monkeypatch.setattr(net, 'request', request)
    monkeypatch.setattr(web.chat, 'text', lambda _chat_id, message: replies.append(message))
    monkeypatch.setattr(web, 'read', lambda *_args: pytest.fail('must not reread a challenge'))
    monkeypatch.setattr(web, 'file_card', lambda *_args: pytest.fail('must not bypass a challenge'))
    web.route(17, url, 'es')
    assert calls == [('GET', url, False, True)]
    assert responses[0].closed and 'verificación' in replies[-1]


def test_mediafire_inspection_is_bounded_and_closes_response(monkeypatch):
    url = 'https://www.mediafire.com/file/fixture/example'
    response = Response(url, body=b'<p>Public share page</p>' + b'x' * 1_000_000)
    calls = []
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy, kwargs.get('stream')))
        return response
    monkeypatch.setattr(net, 'request', request)
    assert files._mediafire(url, stop_on_block=True) == url
    assert calls == [('GET', url, False, True)]
    assert 0 < response.consumed <= 216_384
    assert response.closed


def test_mediafire_unblocked_file_still_resolves_and_shows_card(monkeypatch):
    url = 'https://www.mediafire.com/file/fixture/example'
    download = 'https://download1.mediafire.com/fixture/file.pdf'
    calls, results, responses = [], [], []
    def request(method, target, proxy=False, **kwargs):
        calls.append((target, proxy))
        if target == url:
            response = Response(target, body=f'<a href="{download}">Download</a>'.encode())
        else:
            response = Response(target, body=b'%PDF-1.4\nsynthetic', kind='application/pdf')
        responses.append(response)
        return response
    monkeypatch.setattr(net, 'request', request)
    monkeypatch.setattr(web, 'file_card', lambda _chat_id, info, _locale: results.append(info))
    monkeypatch.setattr(web, 'read', lambda *_args: pytest.fail('PDF should route to file card'))
    web.route(17, url, 'es')
    assert calls == [(url, False), (download, False)]
    assert len(results) == 1 and results[0].kind == 'pdf'
    assert all(response.closed for response in responses)


def test_probe_failure_logs_no_url_query_or_exception_message(monkeypatch, caplog):
    url = 'https://fixture.example/private?token=synthetic-secret'
    def failed(*_args, **_kwargs):
        raise RuntimeError('synthetic-secret in transport exception')
    monkeypatch.setattr(files, 'probe', failed)
    routes = []
    monkeypatch.setattr(web, 'read', lambda _chat_id, target, _locale: routes.append(target))
    with caplog.at_level(logging.INFO, logger='michiflin'):
        web.route(17, url, 'es')
    assert routes == [url]
    assert 'synthetic-secret' not in caplog.text and url not in caplog.text
    assert 'probe failed' in caplog.text


def test_browser_proxy_fallback_logs_no_url_or_sensitive_exception(monkeypatch, caplog):
    monkeypatch.setattr(browser.config, 'WEB_PROXY', 'http://synthetic-proxy.invalid')
    class Gateway:
        def __init__(self, upstream='', **_kwargs):
            self.url = upstream or 'http://direct.invalid'
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
    monkeypatch.setattr(browser.egress, 'EgressProxy', Gateway)
    calls = []
    def work(_browser, _url, proxy):
        calls.append(proxy)
        if len(calls) == 1:
            raise TimeoutError('Timeout https://fixture.example/?token=synthetic-secret')
        return 'synthetic response'
    with caplog.at_level(logging.INFO, logger='michiflin.browser'):
        assert browser._with_proxy_fallback(work)(None, 'https://fixture.example/?token=synthetic-secret') == 'synthetic response'
    assert len(calls) == 2 and 'browser retry' in caplog.text
    assert 'synthetic-secret' not in caplog.text and 'https://fixture.example' not in caplog.text


def test_non_html_reader_fallback_stops_on_429(monkeypatch):
    url, calls, replies = 'https://fixture.example/private.pdf', [], []
    monkeypatch.setattr(reader, 'load', lambda _url: (_ for _ in ()).throw(reader.NotHtml('application/pdf')))
    def request(method, target, proxy=False, **kwargs):
        calls.append((method, target, proxy))
        return Response(target, status=429)
    monkeypatch.setattr(net, 'request', request)
    monkeypatch.setattr(web.chat, 'text', lambda _chat_id, message: replies.append(message))
    monkeypatch.setattr(web, 'file_card', lambda *_args: pytest.fail('blocked probe has no card'))
    web.read(17, url, 'es')
    assert calls == [('GET', url, False)] and 'Espera' in replies[-1]
    assert reader._site_paused(url)
