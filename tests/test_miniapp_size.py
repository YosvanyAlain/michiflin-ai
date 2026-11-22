"""Package budget, lossless source redistribution and real offline engine smoke."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import lzma
from unittest import mock
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from michiflin import app_package, config, miniapp
from tests.test_filekit_archive import INIT

pytest_plugins = ['tests.test_android_startup']  # provides the real extracted-package host


@pytest.mark.parametrize('stem', ['source-7zip-26.05', 'source-wrapper-26.5.0'])
def test_compact_package_carries_exact_full_original_source_stream(stem, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path)
    package = miniapp.build()
    assert package.stat().st_size < 3_800_000
    with zipfile.ZipFile(package) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(miniapp.package_names())
        path = f'vendor/7zip/{stem}.tar.xz'
        assert archive.getinfo(path).compress_type == zipfile.ZIP_STORED
        compact = archive.read(path)
        original = (miniapp.APP_DIR / f'vendor/7zip/{stem}.tar.gz').read_bytes()
        assert lzma.decompress(compact) == gzip.decompress(original)
        notice = archive.read('vendor/7zip/NOTICE.txt').decode('utf-8')
        assert hashlib.sha256(compact).hexdigest() in notice
        assert f'{stem}.tar.xz' in notice
        assert archive.read('vendor/7zip/sevenzip-wasm.wasm') == (miniapp.APP_DIR / 'vendor/7zip/sevenzip-wasm.wasm').read_bytes()
        assert archive.read('vendor/7zip/sevenzip-wasm.js') == (miniapp.APP_DIR / 'vendor/7zip/sevenzip-wasm.js').read_bytes()
        assert b'LGPL' in archive.read('vendor/7zip/LICENSE')


def test_package_hash_tracks_actual_compact_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(miniapp, 'APP_DIR', tmp_path)
    names = ('vendor/7zip/NOTICE.txt', 'vendor/7zip/source-7zip-26.05.tar.gz')
    monkeypatch.setattr(miniapp, 'APP_FILES', names)
    for archive_name, source in app_package.entries(names):
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('first ' + archive_name).encode())
    miniapp._cached_app_build.cache_clear()
    first = miniapp.app_build()
    (tmp_path / 'vendor/7zip/NOTICE-compact.txt').write_text('changed notice')
    second = miniapp.app_build()
    assert second != first
    (tmp_path / 'vendor/7zip/source-7zip-26.05.tar.xz').write_bytes(b'changed compressed source')
    assert miniapp.app_build() != second


def test_concurrent_builds_publish_only_complete_archives(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        targets = list(pool.map(lambda _i: miniapp.build(), range(4)))
    assert len(set(targets)) == 1
    with zipfile.ZipFile(targets[0]) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(miniapp.package_names())
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('as_symlink', [False, True])
def test_compact_source_restore_never_overwrites_unexpected_existing_file(tmp_path, as_symlink):
    from tools.restore_compact_sources import restore

    folder = tmp_path / 'webxdc' / 'vendor' / '7zip'
    folder.mkdir(parents=True)
    target = folder / 'source-7zip-26.05.tar.xz'
    other = tmp_path / 'keep'; other.write_bytes(b'preserve')
    if as_symlink:
        target.symlink_to(other)
    else:
        target.write_bytes(b'preserve')
    with pytest.raises(ValueError, match='not overwritten'):
        restore(tmp_path)
    assert target.read_bytes() == b'preserve' and other.read_bytes() == b'preserve'
    assert target.is_symlink() is as_symlink
    assert not list(folder.glob('*.part'))


def test_failed_build_does_not_leave_a_partial_or_publish_corrupt_zip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path)
    with mock.patch.object(zipfile.ZipFile, 'writestr', side_effect=OSError('synthetic disk failure')):
        with pytest.raises(OSError, match='synthetic disk failure'):
            miniapp.build()
    assert not list(tmp_path.glob('*.part'))
    assert not list(tmp_path.glob('*.xdc'))


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_actual_packaged_offline_wasm_engine_roundtrip(packaged_app, engine):
    _root, url = packaged_app
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(**({'args': ['--no-sandbox']} if engine == 'chromium' else {}))
        try:
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url, wait_until='domcontentloaded')
            page.wait_for_function('window.__michiflinStartupDone')
            page.evaluate(INIT)
            result = page.evaluate("""async () => {
              const made = await runKit({action:'create',format:'zip',name:'sample.zip',
                password:'synthetic',limit:4*1024*1024,
                files:[{path:'hello.txt',blob:new Blob(['offline source test'])}]});
              if (!made.ok) return {made:false,code:made.code};
              const files=made.result.files.map(f=>({path:f.path,blob:new Blob([f.bytes])}));
              const opened=await runKit({action:'extract',files,first:files[0].path,
                password:'synthetic',limit:4*1024*1024});
              return {made:true,opened:opened.ok,code:opened.code,
                text:opened.ok?new TextDecoder().decode(opened.result.files[0].bytes):''};
            }""")
            assert result['made'] and result['opened'], result
            assert result['text'] == 'offline source test'
            assert errors == []
        finally:
            browser.close()
