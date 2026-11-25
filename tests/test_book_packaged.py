"""Exercise readers from the actual built and extracted WebXDC, with synthetic books."""
from __future__ import annotations

import hashlib
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from michiflin import app_package, config, miniapp
from tests.test_book_originals import sample_epub, sample_pdf
from tests.test_epub_internal_links import open_fixture, wait_for_target
from tests.test_welcome_reader_edges import show_reading_area, tap_edge

pytest_plugins = ['tests.test_android_startup']


def test_actual_archive_contains_exact_reader_assets_and_licenses(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TEMP_ROOT', tmp_path)
    archive = miniapp.build()
    assert archive.stat().st_size < 4_600_000
    assets = {name: source for name, source in app_package.entries(miniapp.APP_FILES)}
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None
        assert set(package.namelist()) == set(assets)
        for name, source in assets.items():
            assert package.read(name) == (miniapp.APP_DIR / source).read_bytes(), name
        notice = package.read('vendor/books/NOTICE.txt').decode()
        for name in ('fflate.min.js', 'jszip.min.js', 'epub.min.js', 'purify.min.js', 'pdf.min.mjs', 'pdf.worker.min.mjs'):
            digest = hashlib.sha256(package.read('vendor/books/' + name)).hexdigest()
            assert digest in notice
        assert all('LICENSE-' + name + '.txt' in notice or 'LICENSE-' + name + '.txt' in {
            item.split('/')[-1] for item in package.namelist()}
            for name in ('fflate', 'jszip', 'epubjs', 'dompurify', 'pdfjs'))


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['epub', 'pdf'])
def test_packaged_offline_reader_and_exact_export(packaged_app, engine, kind):
    _root, url = packaged_app
    raw = (sample_epub('<h1>Capítulo empaquetado</h1><img src="https://remote.invalid/pixel"/>'
                       '<script>parent.__book_attack=true</script>') if kind == 'epub' else sample_pdf())
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(**({'args': ['--no-sandbox']} if engine == 'chromium' else {}))
        try:
            page = browser.new_page(viewport={'width': 390, 'height': 844}, locale='es', has_touch=True)
            errors, remote = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def stop_remote(route):
                remote.append(route.request.url)
                route.abort()
            page.route('**://remote.invalid/**', stop_remote)
            page.goto(url, wait_until='domcontentloaded')
            page.wait_for_function('window.__michiflinStartupDone && cache.at>0 && !syncing')
            page.evaluate("go('books',{tab:'saved'})")
            page.locator('.book-file-input').set_input_files({
                'name': 'packaged.' + kind, 'mimeType': 'application/epub+zip' if kind == 'epub' else 'application/pdf', 'buffer': raw})
            page.get_by_role('status').get_by_text('Importados: 1', exact=False).wait_for(timeout=30000)
            page.get_by_role('button', name='Abrir', exact=True).click()
            page.locator('.book-file-status').get_by_text('Lectura sin conexión', exact=False).wait_for(timeout=30000)
            if kind == 'epub':
                iframe = page.frame_locator('.book-file-stage iframe').first
                assert iframe.locator('h1').inner_text() == 'Capítulo empaquetado'
                assert iframe.locator('script').count() == 0
                assert not page.evaluate('window.__book_attack || false')
                show_reading_area(page, '.book-file-stage')
                tap_edge(page, '.book-file-stage', 'right')
                page.wait_for_function("() => document.querySelector('.book-file-page').textContent.includes('Sección 2 / 2')", timeout=15000)
            else:
                assert page.locator('.book-pdf-canvas').evaluate('el=>el.width>0 && el.height>0')
                show_reading_area(page, '.book-file-stage')
                tap_edge(page, '.book-file-stage', 'right')
                page.get_by_text('Página 2 / 3', exact=True).wait_for()
            page.evaluate('''()=>{window.exportSheet=async(blob,name)=>{window.__export={name,hash:await FileKitCrypto.hashBlob(blob),size:blob.size}}}''')
            page.get_by_role('button', name='Exportar archivo original', exact=True).click()
            page.wait_for_function('window.__export && window.__export.hash')
            assert page.evaluate('window.__export') == {'name': 'packaged.' + kind,
                                                       'hash': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
            assert remote == [] and errors == []
        finally:
            browser.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_packaged_epub_links_remain_offline_and_sandboxed(packaged_app, engine):
    _root, url = packaged_app
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(**({'args': ['--no-sandbox']} if engine == 'chromium' else {}))
        try:
            page = browser.new_page(viewport={'width': 320, 'height': 640}, locale='es', has_touch=True)
            errors, remote = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def stop_remote(route):
                remote.append(route.request.url)
                route.abort()
            page.route('**://remote.invalid/**', stop_remote)
            page.goto(url, wait_until='domcontentloaded')
            page.wait_for_function('window.__michiflinStartupDone && cache.at>0 && !syncing')
            _book_id, original_url = open_fixture(page)
            frame = page.frame_locator('.book-file-stage iframe').first
            frame.get_by_text('Saltar en este capítulo', exact=True).tap()
            wait_for_target(page, 'destino uno')
            frame.get_by_text('Volver al comienzo', exact=True).tap()
            wait_for_target(page, 'inicio')
            frame.get_by_text('Ir al segundo capítulo', exact=True).focus()
            page.keyboard.press('Enter')
            page.wait_for_function("document.querySelector('.book-file-page').textContent.includes('Sección 2 / 2')", timeout=15000)
            wait_for_target(page, 'destino-dos')
            assert page.locator('.book-file-stage iframe').first.get_attribute('sandbox') == 'allow-same-origin'
            assert page.url == original_url and not page.evaluate('window.__book_attack || false')
            assert remote == [] and errors == []
        finally:
            browser.close()
