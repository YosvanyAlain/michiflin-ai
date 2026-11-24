"""Original book delivery: synthetic data only; no mail, credentials or live catalogues."""
from __future__ import annotations

import base64
import hashlib
import io
import time
import zipfile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from michiflin import book_library, book_original, miniapp


def sample_epub(chapter=None, *, extra=None):
    chapter = chapter or '<h1>Capítulo uno</h1><p>Texto auténtico con <em>énfasis</em>.</p><img src="image.png" alt="Dibujo"/>'
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr('OEBPS/package.opf', '''<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">synthetic-id</dc:identifier><dc:title>Fixture</dc:title><dc:language>es</dc:language></metadata><manifest><item id="c1" href="one.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="two.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="image" href="image.png" media-type="image/png"/><item id="css" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>''')
        z.writestr('OEBPS/one.xhtml', '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Uno</title><link rel="stylesheet" type="text/css" href="style.css"/></head><body>' + chapter + '</body></html>')
        z.writestr('OEBPS/two.xhtml', '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Dos</title></head><body><h1>Capítulo dos</h1><p>Segunda página de prueba.</p></body></html>')
        z.writestr('OEBPS/nav.xhtml', '''<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Índice</title></head><body><nav epub:type="toc"><ol><li><a href="one.xhtml">Capítulo uno</a></li><li><a href="two.xhtml">Capítulo dos</a></li></ol></nav></body></html>''')
        z.writestr('OEBPS/image.png', png)
        z.writestr('OEBPS/style.css', 'body { color: #274a3c; } @import url(https://remote.invalid/evil.css); p { background-image: url(https://remote.invalid/pixel); }')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return output.getvalue()


def sample_pdf():
    writer = PdfWriter()
    for number in range(1, 4):
        page = writer.add_blank_page(width=400, height=600)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject(); stream.set_data(f'BT /F1 22 Tf 40 500 Td (Synthetic page {number}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO(); writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(book_original, '_sessions', {})


def test_exact_bytes_parts_are_repeatable_and_bound_to_instance(monkeypatch):
    raw = (b'Texto original \xc3\xb1.\n' * 60000) + b'last byte'
    assert len(raw) > book_original.PART_BYTES
    fetch = []
    monkeypatch.setattr(book_library, '_fetch', lambda url, maximum, guard, **kwargs: (fetch.append((url, maximum, guard, kwargs)) or (raw, 'text/plain')))
    first = book_original.start('a' * 64, 'https://example.org/book.txt', 'txt')
    assert first['size'] == len(raw) and first['sha256'] == hashlib.sha256(raw).hexdigest() and first['count'] == 2
    second = book_original.part('a' * 64, first['transfer'], 1)
    assert second == book_original.part('a' * 64, first['transfer'], 1)
    assert base64.b64decode(first['data']) + base64.b64decode(second['data']) == raw
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.part('b' * 64, first['transfer'], 1)
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.part('a' * 64, first['transfer'], True)
    assert len(fetch) == 1 and fetch[0][2] is True and fetch[0][3] == {'anonymous': True}
    book_original.close('b' * 64, first['transfer'])  # a foreign close cannot remove it
    assert book_original.part('a' * 64, first['transfer'], 0)['data'] == first['data']
    assert book_original.close('a' * 64, first['transfer']) == {'closed': True}
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.part('a' * 64, first['transfer'], 0)


def test_expiry_restart_limits_and_public_fetch_fail_closed(monkeypatch):
    monkeypatch.setattr(book_library, '_fetch', lambda *args, **kwargs: (b'hello', 'text/plain'))
    first = book_original.start('c' * 64, 'https://example.org/a.txt', 'txt')
    book_original._sessions[first['transfer']]['at'] = time.monotonic() - book_original.TTL_SECONDS - 1
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.part('c' * 64, first['transfer'], 0)
    second = book_original.start('c' * 64, 'https://example.org/a.txt', 'txt')
    book_original._sessions.clear()  # simulated bot restart
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.part('c' * 64, second['transfer'], 0)
    monkeypatch.setattr(book_original, 'MAX_SESSIONS', 1)
    book_original.start('c' * 64, 'https://example.org/a.txt', 'txt')
    with pytest.raises(book_library.BookError, match='source_temporarily_unavailable'):
        book_original.start('c' * 64, 'https://example.org/b.txt', 'txt')
    with pytest.raises(book_library.BookError, match='transfer_expired'):
        book_original.start('untrusted scope', 'https://example.org/a.txt', 'txt')


@pytest.mark.parametrize('kind,raw,content', [('epub', sample_epub(), 'application/epub+zip'), ('pdf', sample_pdf(), 'application/pdf'), ('txt', b'\xef\xbb\xbfHello\r\n', 'text/plain')])
def test_start_returns_original_bytes_never_a_reconstruction(monkeypatch, kind, raw, content):
    monkeypatch.setattr(book_library, '_fetch', lambda *args, **kwargs: (raw, content))
    response = miniapp.op_books(1, {'do': 'original_start', '_book_scope': 'd' * 64, 'url': 'https://example.org/book.' + kind, 'format': kind}, 'es')
    assert base64.b64decode(response['data']) == raw
    assert response['sha256'] == hashlib.sha256(raw).hexdigest()
    assert response['format'] == kind


def test_html_and_malformed_archives_are_not_accepted(monkeypatch):
    with pytest.raises(book_library.BookError, match='not_a_book_epub'):
        book_original._validate(b'<html><body>not a book</body></html>', 'text/html', 'epub')
    with pytest.raises(book_library.BookError, match='epub_structure'):
        book_original._validate(b'PK\x03\x04not a zip', 'application/epub+zip', 'epub')
    evil = sample_epub(extra={'../escape.txt': b'not allowed'})
    with pytest.raises(book_library.BookError, match='epub_structure'):
        book_original._validate(evil, 'application/epub+zip', 'epub')
    with pytest.raises(book_library.BookError, match='not_a_book_pdf'):
        book_original._validate(b'not a PDF', 'application/pdf', 'pdf')


def test_encrypted_or_font_obfuscated_original_can_be_preserved_without_bypass(monkeypatch):
    # encryption.xml is also used for font obfuscation, not proof of DRM.
    raw = sample_epub(extra={'META-INF/encryption.xml': b'<encryption/>'})
    monkeypatch.setattr(book_library, '_fetch', lambda *args, **kwargs: (raw, 'application/epub+zip'))
    first = book_original.start('e' * 64, 'https://example.org/book.epub', 'epub')
    assert base64.b64decode(first['data']) == raw


def test_maximum_original_size_and_total_session_budget(monkeypatch):
    book_original._validate(b'a' * book_library.MAX_DOCUMENT, 'text/plain', 'txt')
    with pytest.raises(book_library.BookError, match='book_too_large'):
        book_original._validate(b'a' * (book_library.MAX_DOCUMENT + 1), 'text/plain', 'txt')
    monkeypatch.setattr(book_library, '_fetch', lambda *_args, **_kwargs: (b'1234567', 'text/plain'))
    monkeypatch.setattr(book_original, 'MAX_TOTAL', 12)
    first = book_original.start('f' * 64, 'https://example.invalid/one.txt', 'txt')
    with pytest.raises(book_library.BookError, match='source_temporarily_unavailable'):
        book_original.start('f' * 64, 'https://example.invalid/two.txt', 'txt')
    assert len(book_original._sessions) == 1
    book_original.close('f' * 64, first['transfer'])
    assert book_original.start('f' * 64, 'https://example.invalid/two.txt', 'txt')['size'] == 7


def test_concurrent_fetch_cap_is_released_even_when_busy(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock, all_entered, release = threading.Lock(), threading.Event(), threading.Event()
    active = []
    monkeypatch.setattr(book_original, '_fetch_slots', threading.BoundedSemaphore(4))
    def fetch(*_args, **_kwargs):
        with lock:
            active.append(1)
            if len(active) == 4:
                all_entered.set()
        if not release.wait(10):
            raise RuntimeError('test did not release fetch')
        return b'synthetic', 'text/plain'
    monkeypatch.setattr(book_library, '_fetch', fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(book_original.start, 'c' * 64, 'https://example.invalid/book.txt', 'txt') for _ in range(4)]
        try:
            assert all_entered.wait(5)
            with pytest.raises(book_library.BookError, match='source_temporarily_unavailable'):
                book_original.start('c' * 64, 'https://example.invalid/fifth.txt', 'txt')
        finally:
            release.set()
        assert all(item.result(timeout=10)['size'] == 9 for item in pending)
    assert book_original.start('c' * 64, 'https://example.invalid/after.txt', 'txt')['size'] == 9
