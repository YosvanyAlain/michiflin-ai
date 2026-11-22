"""Regression for the requested existing YouTube video/audio paths.

All media and provider responses are synthetic. No real account, download,
mail, or production data is used.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from michiflin import config, media, private_media
from tests.test_webxdc_browser import browser_fixture


@pytest.mark.parametrize('engine,locale', [('chromium', 'es'), ('webkit', 'en')])
@pytest.mark.parametrize('kind,quality,fmt', [('video', 720, 'mp4'), ('audio', 128, 'mp3')])
def test_existing_youtube_actions_are_available_without_an_account_flow(engine, locale, kind, quality, fmt):
    url = 'https://www.youtube.com/watch?v=abcdefghijk'
    with browser_fixture(locale, {'width': 390, 'height': 844}, engine=engine, touch=True) as (page, errors):
        page.wait_for_function('cache.at > 0')
        page.evaluate("""url => {
          store.set('youtube.showThumbnails', false);
          window.__searchResults = [{title:'Synthetic video', url, source:'YouTube'}];
          go('youtube', {q:'synthetic'});
        }""", url)
        card = page.locator('.video-body').filter(has_text='Synthetic video')
        card.wait_for()
        label = page.evaluate("kind => t(kind === 'video' ? 'act_video' : 'act_audio')", kind)
        card.get_by_role('button', name=label, exact=True).click()
        sheet = page.locator('#sheet')
        quality_label = f'{quality}p' if kind == 'video' else f'{quality} kbps'
        sheet.get_by_role('button', name=quality_label, exact=True).click()
        sheet.get_by_role('button', name=page.evaluate("t('start_download')"), exact=True).click()
        page.wait_for_function("kind => window.__sentUpdates.some(x => x.payload?.op === 'action' && x.payload.args.kind === kind)", arg=kind)
        actions = page.evaluate("window.__sentUpdates.filter(x => x.payload?.op === 'action').map(x => x.payload.args)")
        assert len(actions) == 1
        assert actions[0]['url'] == url
        assert actions[0]['kind'] == kind
        assert actions[0]['quality'] == quality
        assert actions[0]['video_format'] == (fmt if kind == 'video' else '')
        assert actions[0]['audio_format'] == (fmt if kind == 'audio' else '')
        assert page.evaluate("!('official_connections' in VIEWS)")
        assert page.evaluate("!window.__sentUpdates.some(x => ['youtube_account','facebook_account','tiktok_account'].includes(x.payload?.op))")
        assert errors == []


@pytest.mark.parametrize('kind,quality,fmt', [('video', 720, 'mp4'), ('audio', 128, 'mp3')])
def test_downloader_receives_the_requested_youtube_format_without_provider_network(monkeypatch, tmp_path, kind, quality, fmt):
    received = []

    class FakeGateway:
        def __init__(self, **_kwargs):
            self.url = 'http://fixture-gateway.invalid'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, urls):
            received.append((urls, self.options))
            Path(self.options['outtmpl'].replace('%(ext)s', fmt)).write_bytes(b'synthetic-media')

    fake = SimpleNamespace(YoutubeDL=FakeYDL, utils=SimpleNamespace(DownloadCancelled=type('Cancelled', (Exception,), {})))
    monkeypatch.setattr(media, 'yt_dlp', fake, raising=False)
    monkeypatch.setattr(media, 'supported', lambda _url: True)
    monkeypatch.setattr(media.net, 'validate_public_url', lambda value: value)
    monkeypatch.setattr(private_media, 'storage_for', lambda _url: None)
    monkeypatch.setattr(media.egress, 'EgressProxy', FakeGateway)
    monkeypatch.setattr(config, 'MAX_MEDIA_MINUTES', 0)
    monkeypatch.setattr(config, 'MEDIA_PROXY', '')
    monkeypatch.setattr(config, 'YTDLP_COOKIES', '')
    url = 'https://www.youtube.com/watch?v=abcdefghijk'
    result = media.download(url, tmp_path, kind, quality, fmt)
    assert result.read_bytes() == b'synthetic-media'
    assert result.suffix == '.' + fmt
    urls, options = received[0]
    assert urls == [url] and len(received) == 1
    if kind == 'video':
        assert options['merge_output_format'] == fmt
        assert f'res:{quality}' in options['format_sort']
    else:
        assert options['postprocessors'][0]['preferredcodec'] == fmt
        assert options['postprocessors'][0]['preferredquality'] == str(quality)
