"""A book original and its public-archive check must never inherit cookies."""
from __future__ import annotations

import json

from michiflin import book_library, book_original, net


def test_public_original_and_archive_metadata_use_fresh_anonymous_sessions(monkeypatch):
    sessions, gateways = [], []
    metadata = {'metadata': {'mediatype': 'texts', 'identifier': 'fixture'},
                'files': [{'name': 'book.txt', 'format': 'Text', 'size': '19'}]}

    class FakeResponse:
        def __init__(self, code, body, headers):
            self.status_code, self.body, self.headers, self.closed = code, body, headers, False
        def iter_content(self, chunk_size):
            yield self.body
        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError('unexpected synthetic response')
        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.headers, self.cookies, self.trust_env, self.closed = {}, {}, True, False
            sessions.append(self)
        def request(self, method, url, **kwargs):
            assert self.trust_env is False
            assert not self.cookies and 'Authorization' not in self.headers
            assert kwargs['proxies'] == {'http': 'http://synthetic-gateway', 'https': 'http://synthetic-gateway'}
            if '/metadata/' in url:
                return FakeResponse(200, json.dumps(metadata).encode(), {'content-type': 'application/json'})
            if url == 'https://archive.org/download/fixture/book.txt':
                return FakeResponse(302, b'', {'Location': 'https://files.example.invalid/book.txt'})
            assert url == 'https://files.example.invalid/book.txt'
            return FakeResponse(200, b'Public original.\n', {'content-type': 'text/plain'})
        def close(self):
            self.closed = True

    class FakeGateway:
        url = 'http://synthetic-gateway'
        def __init__(self, **_kwargs):
            self.closed = False
            gateways.append(self)
        def close(self):
            self.closed = True

    def no_private_session():
        raise AssertionError('private browsing session must not be used')

    monkeypatch.setattr(book_original, '_sessions', {})
    monkeypatch.setattr(book_library, '_source_pause', {})
    monkeypatch.setattr(book_library, '_source_pause_reason', {})
    monkeypatch.setattr(net, 'session', no_private_session)
    monkeypatch.setattr(net, 'validate_public_url', lambda url: url)
    monkeypatch.setattr(net.requests, 'Session', FakeSession)
    monkeypatch.setattr(net.egress, 'EgressProxy', FakeGateway)
    result = book_original.start('a' * 64, 'https://archive.org/download/fixture/book.txt', 'txt')
    assert result['size'] == len(b'Public original.\n')
    assert len(sessions) == 3  # metadata, redirect, and final file each get a fresh session
    assert all(item.closed for item in sessions) and all(item.closed for item in gateways)
    assert all(item.trust_env is False and not item.cookies for item in sessions)
