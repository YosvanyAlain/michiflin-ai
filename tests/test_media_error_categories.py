"""Synthetic diagnosis only: categories are not a verified live YouTube download."""
import pytest

from michiflin import media, media_worker, queue


@pytest.mark.parametrize('message,expected', [
    ('proxy_unavailable', 'proxy_unavailable'), ('proxy_invalid', 'proxy_invalid'),
    ('isolation_unavailable', 'isolation_unavailable'),
    ('network unavailable', 'network'), ('Network is unreachable', 'network'),
    ('Temporary failure in name resolution', 'network'), ('Connection reset by peer', 'network'),
    ('Unable to download webpage: timed out', 'network'),
    ('SOCKS5 proxy connection refused', 'proxy_unavailable'),
    ('HTTP Error 403: Forbidden', 'http_forbidden'),
    ('Unable to download webpage: HTTP Error 403: Forbidden', 'http_forbidden'),
    ('Unable to download webpage: HTTP Error 429: Too Many Requests', 'rate_limited'), ('HTTP Error 429: Too Many Requests', 'rate_limited'),
    ('HTTP Error 403: Sign in to confirm you are not a bot', 'bot_check'),
    ('Requested format is not available', 'format_unavailable'),
    ('No supported JavaScript runtime could be found', 'javascript_missing'),
    ('ffmpeg not found', 'ffmpeg_missing'), ('[Errno 28] No space left on device', 'disk_full'),
    ('Video unavailable', 'unavailable'), ('Private video: sign in', 'private'),
])
def test_network_proxy_and_provider_denials_are_distinct_in_both_workers(message, expected):
    assert media.error_code(RuntimeError(message)) == expected
    assert media_worker.fail(RuntimeError(message)) == expected


def test_messages_do_not_claim_warp_failed_or_another_device_will_authorize_server():
    assert 'no demuestra' in media.describe_error('network', 'es')
    assert 'does not prove' in media.describe_error('http_forbidden', 'en')
    assert 'no siempre autoriza' in media.describe_error('bot_check', 'es')
    assert 'proxy no está disponible' in media.describe_error('proxy_unavailable', 'es')
    assert '403' in queue._error_text('http_forbidden', 'es')
    assert '429' in queue._error_text('rate_limited', 'es')


def test_no_cookies_or_urls_in_public_categories():
    exception = RuntimeError('HTTP Error 403 at https://private.invalid/signed?token=SECRET cookie=session')
    code = media.error_code(exception)
    assert code == 'http_forbidden'
    assert 'SECRET' not in media.describe_error(code, 'es')


def test_main_extractor_has_the_same_supported_js_runtimes(monkeypatch):
    monkeypatch.setattr(media, '_cookies', lambda: '')
    assert set(media._options()['js_runtimes']) == {'deno', 'node'}
