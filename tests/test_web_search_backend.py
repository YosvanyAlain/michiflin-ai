"""Deterministic search outcomes; no real providers or user queries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from michiflin import miniapp, tools


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tools, '_cache', {})
    monkeypatch.setattr(tools, '_search_paused_until', 0.0)
    monkeypatch.setattr(tools, 'DDGS_ENGINES', {'text': {'duckduckgo': object(), 'brave': object(), 'wikipedia': object()}})
    tools.clear_search_status()
    yield
    tools.clear_search_status()


def test_removed_backend_never_triggers_ddgs_random_auto_mode(monkeypatch):
    attempts = []
    class Fake:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **kwargs):
            attempts.append(kwargs['backend'])
            return [{'href': 'https://fixture.example/result', 'title': 'A result', 'body': 'Summary'}]
    monkeypatch.setattr(tools, 'DDGS', Fake)
    assert tools._text_search_backends() == ('duckduckgo', 'brave')
    assert tools.search('synthetic query', 'en', 5)[0].url == 'https://fixture.example/result'
    assert attempts == ['duckduckgo'] and tools.search_status() == 'ok'
    assert 'bing' not in attempts and 'auto' not in attempts and 'wikipedia' not in attempts


def test_missing_registry_fails_closed_instead_of_random_auto(monkeypatch):
    monkeypatch.setattr(tools, 'DDGS_ENGINES', {'text': {'wikipedia': object()}})
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('unsupported backend attempted'))
    with pytest.raises(miniapp.AppError, match='no respondió'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'unavailable'


def test_rate_limit_stops_every_fallback_and_is_explained(monkeypatch):
    attempts = []
    class Challenged:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **kwargs):
            attempts.append(kwargs['backend'])
            raise RuntimeError('HTTP 429 CAPTCHA')
    monkeypatch.setattr(tools, 'DDGS', Challenged)
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'another synthetic query', 'engine': 'web'}, 'es')
    assert attempts == ['duckduckgo'] and tools.search_limited()
    assert tools.search_status() == 'limited'


def test_ambiguous_empty_exception_never_switches_provider_or_claims_confirmed_outage(monkeypatch):
    attempts = []
    class Ambiguous:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **kwargs):
            attempts.append((kwargs['region'], kwargs['backend']))
            raise RuntimeError('No results found.')  # DDGS may hide a 429 or CAPTCHA here
    monkeypatch.setattr(tools, 'DDGS', Ambiguous)
    with pytest.raises(miniapp.AppError, match='también podría no estar disponible'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'indeterminate' and attempts == [('xl-es', 'duckduckgo')]
    text = tools.search_text('another synthetic query', 'es')
    assert 'No se obtuvieron resultados' in text and 'podría' in text
    assert attempts == [('xl-es', 'duckduckgo'), ('xl-es', 'duckduckgo')]


def test_empty_response_is_ambiguous_and_does_not_try_another_provider(monkeypatch):
    attempts = []
    class Empty:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **kwargs):
            attempts.append(kwargs['backend'])
            return []
    monkeypatch.setattr(tools, 'DDGS', Empty)
    with pytest.raises(miniapp.AppError, match='también podría no estar disponible'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'indeterminate' and attempts == ['duckduckgo']


def test_successful_but_irrelevant_results_remain_no_results(monkeypatch):
    class Irrelevant:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **_kwargs):
            return [{'href': 'https://en.wikipedia.org/wiki/Other', 'title': 'Unrelated', 'body': 'Other'}]
    monkeypatch.setattr(tools, 'DDGS', Irrelevant)
    reply = miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert reply['results'] == [] and tools.search_status() == 'empty'
    assert 'Sin resultados' in tools.search_text('another synthetic query', 'es')


def test_provider_errors_are_generic_and_do_not_echo_secrets(monkeypatch):
    class Broken:
        def __init__(self, **_kwargs):
            pass
        def text(self, _query, **_kwargs):
            raise RuntimeError('sensitive-token-value synthetic error')
    monkeypatch.setattr(tools, 'DDGS', Broken)
    with pytest.raises(miniapp.AppError) as problem:
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert 'no respondió' in str(problem.value) and 'sensitive-token-value' not in str(problem.value)
    assert tools.search_status() == 'unavailable'


def test_status_is_thread_local_and_cached_success_resets_a_previous_error(monkeypatch):
    class Fake:
        def __init__(self, **_kwargs):
            pass
        def text(self, query, **_kwargs):
            if query == 'empty':
                return []
            raise RuntimeError('network unavailable')
    monkeypatch.setattr(tools, 'DDGS', Fake)
    barrier = threading.Barrier(2)
    def run(query):
        barrier.wait(timeout=5)
        tools.search(query, 'en', 3)
        barrier.wait(timeout=5)
        return tools.search_status()
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(run, ['empty', 'broken']))
    assert statuses == ['indeterminate', 'unavailable']
    tools._search_local.status = 'unavailable'
    tools._cache['s:en:3:cached'] = (time.monotonic(), [tools.Result('Cached', 'https://fixture.example/a')])
    assert tools.search('cached', 'en', 3)[0].title == 'Cached'
    assert tools.search_status() == 'ok'


def test_advanced_rate_limit_stops_before_switching_provider_or_using_cache(monkeypatch):
    seen = []
    class Limited:
        ok, status_code = False, 429
    monkeypatch.setattr(tools.config, 'YDC_API_KEY', 'synthetic-key')
    monkeypatch.setattr(tools.state, 'usage_today', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'cooldown_until', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'record_usage', lambda *_args: None)
    monkeypatch.setattr(tools.state, 'set_cooldown', lambda *_args: None)
    monkeypatch.setattr(tools.requests, 'get', lambda *_a, **_kw: seen.append('you') or Limited())
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('must not switch after rate limit'))
    tools._cache['s:es:20:cached'] = (time.monotonic(), [tools.Result('Cached', 'https://fixture.example/a')])
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web', 'mode': 'advanced'}, 'es')
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'cached', 'engine': 'web'}, 'es')
    assert seen == ['you'] and tools.search_limited()


def test_advanced_daily_cap_and_error_do_not_silently_switch(monkeypatch):
    monkeypatch.setattr(tools.config, 'YDC_API_KEY', 'synthetic-key')
    monkeypatch.setattr(tools.config, 'YDC_DAILY', 5)
    monkeypatch.setattr(tools.state, 'usage_today', lambda _key: 5)
    monkeypatch.setattr(tools.requests, 'get', lambda *_a, **_kw: pytest.fail('quota exhausted'))
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('must not change provider'))
    with pytest.raises(miniapp.AppError, match='limita las solicitudes'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web', 'mode': 'advanced'}, 'es')
    monkeypatch.setattr(tools.state, 'usage_today', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'cooldown_until', lambda _key: 0)
    monkeypatch.setattr(tools.state, 'set_cooldown', lambda *_args: None)
    def failed(*_args, **_kwargs):
        raise tools.requests.ConnectionError('synthetic network failure')
    monkeypatch.setattr(tools.requests, 'get', failed)
    with pytest.raises(miniapp.AppError, match='no respondió'):
        miniapp.op_search(1, {'q': 'second query', 'engine': 'web', 'mode': 'advanced'}, 'es')


def test_queued_search_waits_and_never_calls_another_provider_after_challenge(monkeypatch):
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    class SlowChallenge:
        def __init__(self, **_kwargs):
            pass
        def text(self, query, **kwargs):
            attempts.append((query, kwargs['backend']))
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError('HTTP 429 CAPTCHA')
    monkeypatch.setattr(tools, 'DDGS', SlowChallenge)
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
    assert attempts == [('first', 'duckduckgo')]


def test_queued_search_has_bounded_wait_and_makes_no_late_request(monkeypatch):
    class BusyLock:
        def acquire(self, timeout):
            assert timeout == 5
            return False
        def release(self):
            pytest.fail('unacquired lock must not be released')
    monkeypatch.setattr(tools, '_search_request_lock', BusyLock())
    monkeypatch.setattr(tools, 'DDGS', lambda **_kwargs: pytest.fail('busy search must not contact provider'))
    with pytest.raises(miniapp.AppError, match='otra búsqueda en curso'):
        miniapp.op_search(1, {'q': 'synthetic query', 'engine': 'web'}, 'es')
    assert tools.search_status() == 'busy'
