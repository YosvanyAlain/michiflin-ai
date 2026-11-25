"""Web search outcomes with a synthetic MCP service; no user queries or paid API calls."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from michiflin import miniapp, tools


class Response:
    def __init__(self, status=200, value=None, *, sse=True, headers=None):
        self.status_code = status
        self.headers = {'Content-Type': 'text/event-stream' if sse else 'application/json', **(headers or {})}
        self.body = (('event: message\ndata: ' if sse else '') + json.dumps(value or {}) + ('\n\n' if sse else '')).encode()
        self.closed = False

    def iter_content(self, _size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *, call_status=200, result=None, init_status=200):
        self.calls = []
        self.call_status, self.init_status = call_status, init_status
        self.result = result if result is not None else {'structuredContent': {'results': {'web': [
            {'url': 'https://fixture.example/result', 'title': 'A result', 'description': 'Summary'}]}}}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        req = kwargs['json']
        if req['method'] == 'initialize':
            return Response(self.init_status, {'jsonrpc': '2.0', 'id': 1, 'result': {}}, headers={'Mcp-Session-Id': 'test-session'})
        if req['method'] == 'notifications/initialized':
            return Response(202)
        return Response(self.call_status, {'jsonrpc': '2.0', 'id': 2, 'result': self.result})


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tools, '_cache', {})
    monkeypatch.setattr(tools, '_search_paused_until', 0.0)
    monkeypatch.setattr(tools.state, 'usage_today', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'cooldown_until', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'cooldown_reason', lambda _key: '')
    monkeypatch.setattr(tools.state, 'record_usage', Mock())
    monkeypatch.setattr(tools.state, 'set_cooldown', Mock())
    monkeypatch.setattr(tools, 'DDGS_ENGINES', {'text': {'duckduckgo': object(), 'brave': object()}})
    tools.clear_search_status()
    yield
    tools.clear_search_status()


def test_standard_uses_official_keyless_profile_and_returns_results(monkeypatch):
    session = Session()
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('must not switch providers'))
    result = miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert result['results'][0]['url'] == 'https://fixture.example/result'
    assert result['results'][0]['source'] == 'You.com'
    assert tools.search_status() == 'ok'
    assert [row[1]['json']['method'] for row in session.calls] == ['initialize', 'notifications/initialized', 'tools/call']
    assert all(url == 'https://api.you.com/mcp/free' for url, _ in session.calls)
    assert all(not any(k.lower() == 'authorization' for k in kw['headers']) for _, kw in session.calls)
    assert all(not kw['allow_redirects'] and kw['stream'] for _, kw in session.calls)
    assert session.calls[-1][1]['json']['params']['arguments']['query'] == 'synthetic query'
    tools.state.record_usage.assert_called_once_with('you:free-mcp', True)
    assert tools.search('synthetic query', 'es', 20)[0].url == 'https://fixture.example/result'
    assert len(session.calls) == 3  # a repeated successful query uses the bounded cache


def test_highlight_and_content_text_shape_are_supported(monkeypatch):
    body = {'results': {'web': [{'url': 'https://fixture.example/a', 'title': 'A',
        'contents': {'highlights': ['A short summary']}}]}}
    session = Session(result={'content': [{'type': 'text', 'text': json.dumps(body)}]})
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    result = tools.search('query', 'en', 5)
    assert result[0].snippet == 'A short summary' and tools.search_status() == 'ok'


def test_no_results_and_fuzzy_unrelated_encyclopedia_are_not_provider_errors(monkeypatch):
    session = Session(result={'structuredContent': {'results': {'web': [
        {'url': 'https://en.wikipedia.org/wiki/Other', 'title': 'Unrelated'}]}}})
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    reply = miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert reply['results'] == [] and tools.search_status() == 'empty'


def test_bad_response_and_redirect_fail_closed_without_provider_hopping(monkeypatch):
    session = Session(init_status=302)
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('must not switch providers'))
    with pytest.raises(miniapp.AppError, match='no respondió'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert len(session.calls) == 1 and tools.search_status() == 'unavailable'
    tools.state.record_usage.assert_not_called()


def test_rate_limit_stops_all_fallback_and_pauses_future_searches(monkeypatch):
    session = Session(call_status=429)
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('must not switch providers'))
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'another query', 'engine': 'web'}, 'es')
    assert len(session.calls) == 3 and tools.search_limited()
    tools.state.set_cooldown.assert_called_once_with('you:free-mcp', 21600, 'limited')
    tools.state.record_usage.assert_called_once_with('you:free-mcp', True)


def test_local_free_quota_fails_before_network(monkeypatch):
    monkeypatch.setattr(tools.state, 'usage_today', lambda _key: tools._FREE_SEARCH_DAILY)
    monkeypatch.setattr(tools.requests, 'Session', lambda: pytest.fail('quota exhausted'))
    assert tools.search('synthetic', 'en') == []
    assert tools.search_status() == 'limited'


def test_mcp_error_details_are_not_disclosed(monkeypatch):
    session = Session(result={'isError': True, 'content': [{'text': 'sensitive-token-value synthetic error'}]})
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    with pytest.raises(miniapp.AppError) as problem:
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert 'no respondió' in str(problem.value) and 'sensitive-token-value' not in str(problem.value)
    assert tools.search_status() == 'unavailable'


def test_status_is_thread_local_and_cached_success_resets_previous_error(monkeypatch):
    barrier = threading.Barrier(2)
    def run(status):
        tools._search_local.status = status
        barrier.wait(timeout=5)
        return tools.search_status()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ['empty', 'unavailable'])) == ['empty', 'unavailable']
    tools._search_local.status = 'unavailable'
    tools._cache['s:en:3:cached'] = (time.monotonic(), [tools.Result('Cached', 'https://fixture.example/a')])
    assert tools.search('cached', 'en', 3)[0].title == 'Cached'
    assert tools.search_status() == 'ok'


def test_advanced_rate_limit_never_falls_back_to_free_provider(monkeypatch):
    class Limited:
        ok, status_code = False, 429
    monkeypatch.setattr(tools.config, 'YDC_API_KEY', 'synthetic-key')
    monkeypatch.setattr(tools.requests, 'get', lambda *_a, **_kw: Limited())
    monkeypatch.setattr(tools.requests, 'Session', lambda: pytest.fail('must not switch provider'))
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web', 'mode': 'advanced'}, 'es')
    assert tools.search_limited()


def test_advanced_missing_key_does_not_silently_use_free_provider(monkeypatch):
    monkeypatch.setattr(tools.config, 'YDC_API_KEY', '')
    monkeypatch.setattr(tools.requests, 'Session', lambda: pytest.fail('must not switch provider'))
    with pytest.raises(miniapp.AppError, match='no respondió'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web', 'mode': 'advanced'}, 'es')


def test_queued_search_never_starts_after_challenge(monkeypatch):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    def challenge(query, _locale, _limit):
        attempts.append(query)
        entered.set()
        assert release.wait(timeout=5)
        tools._pause_search()
        return []
    monkeypatch.setattr(tools, '_free_search', challenge)
    def first():
        return tools.search('first', 'en'), tools.search_status()
    def second():
        second_started.set()
        return tools.search('second', 'en'), tools.search_status()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert entered.wait(timeout=5)
        b = pool.submit(second)
        assert second_started.wait(timeout=5)
        release.set()
        assert a.result(timeout=5) == ([], 'limited')
        assert b.result(timeout=5) == ([], 'limited')
    assert attempts == ['first']


def test_queued_search_has_bounded_wait_and_makes_no_late_request(monkeypatch):
    class BusyLock:
        def acquire(self, timeout):
            assert timeout == 5
            return False
        def release(self):
            pytest.fail('unacquired lock must not be released')
    monkeypatch.setattr(tools, '_search_request_lock', BusyLock())
    monkeypatch.setattr(tools.requests, 'Session', lambda: pytest.fail('busy search must not contact provider'))
    with pytest.raises(miniapp.AppError, match='otra búsqueda en curso'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'busy'


def test_free_profile_does_not_treat_a_challenge_as_zero_results(monkeypatch):
    session = Session(result={'isError': True, 'content': [{'text': 'HTTP 429 CAPTCHA'}]})
    monkeypatch.setattr(tools.requests, 'Session', lambda: session)
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'limited' and tools.search_limited()
